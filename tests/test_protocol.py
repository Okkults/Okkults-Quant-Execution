"""Protocol tests — payload validation, stale rejection, reconnect behaviour."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from signals.protocol import ET, BatchSignal, EntrySignal, is_stale, parse_exit_signal

FRESH_CUTOFF_STAMP = "2099-12-31T15:58:00-05:00"


def valid_signal(i: int = 0, cutoff: str = FRESH_CUTOFF_STAMP) -> dict:
    return {
        "signal_id": f"sig-{i:02d}",
        "ticker": f"T{i:02d}",
        "side": "BUY",
        "reference_price": 208.0,
        "limit_price": None,
        "entry_cutoff_at": cutoff,
        "exit_rules": {"disaster_stop_pct": 0.07},
        "batch_id": "B-001",
    }


def valid_batch(n: int = 3) -> dict:
    return {
        "event": "BATCH_SIGNAL",
        "batch_id": "B-001",
        "signals": [valid_signal(i) for i in range(n)],
    }


class TestBatchPayloadValidation:
    def test_valid_batch_parses(self) -> None:
        batch = BatchSignal.model_validate(valid_batch())
        assert batch.batch_id == "B-001"
        assert len(batch.signals) == 3
        assert batch.signals[0].exit_rules.disaster_stop_pct == 0.07

    def test_missing_signal_id_rejected(self) -> None:
        payload = valid_batch(1)
        payload["signals"][0].pop("signal_id")
        with pytest.raises(ValidationError):
            BatchSignal.model_validate(payload)

    def test_max_twelve_signals(self) -> None:
        assert len(BatchSignal.model_validate(valid_batch(12)).signals) == 12
        with pytest.raises(ValidationError):
            BatchSignal.model_validate(valid_batch(13))
        with pytest.raises(ValidationError):
            BatchSignal.model_validate(valid_batch(0))

    def test_malformed_exit_rules_rejected(self) -> None:
        payload = valid_batch(1)
        payload["signals"][0]["exit_rules"] = {"profit_target_pct": 0.2}
        with pytest.raises(ValidationError):
            BatchSignal.model_validate(payload)

    def test_negative_stop_rejected(self) -> None:
        payload = valid_batch(1)
        payload["signals"][0]["exit_rules"] = {"disaster_stop_pct": -0.07}
        with pytest.raises(ValidationError):
            BatchSignal.model_validate(payload)

    def test_invalid_side_rejected(self) -> None:
        payload = valid_batch(1)
        payload["signals"][0]["side"] = "SHORT"
        with pytest.raises(ValidationError):
            BatchSignal.model_validate(payload)

    def test_exit_signal_validates(self) -> None:
        exit_signal = parse_exit_signal(
            {"action": "EXIT_ALL", "reason": "POST_EARNINGS_OPEN_CROSS"}
        )
        assert exit_signal.reason == "POST_EARNINGS_OPEN_CROSS"

        with pytest.raises(ValidationError):
            parse_exit_signal({"action": "PARTIAL", "reason": "x"})


class TestStaleRejection:
    def test_future_cutoff_is_fresh(self) -> None:
        future = datetime.now(tz=UTC) + timedelta(days=1)
        assert is_stale(future.isoformat()) is False

    def test_past_cutoff_is_stale(self) -> None:
        past = datetime.now(tz=UTC) - timedelta(seconds=10)
        assert is_stale(past.isoformat()) is True

    def test_et_naive_timestamp_is_walled(self) -> None:
        wall = datetime.now(tz=ET) - timedelta(minutes=5)
        assert is_stale(wall.isoformat()) is True

    def test_unparsable_cutoff_treated_stale(self) -> None:
        assert is_stale("not-a-timestamp") is True

    def test_entry_signal_model_defaults(self) -> None:
        entry = EntrySignal.model_validate(valid_signal())
        assert entry.reference_price == 208.0


class TestReconnectBackoff:
    def test_exponential_sequence(self) -> None:
        from signals.listener import RECONNECT_MAX_S, backoff_delay

        delays = [backoff_delay(n) for n in range(6)]
        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
        assert backoff_delay(20) == RECONNECT_MAX_S  # 30s hard cap

    def test_negative_attempt_rejected(self) -> None:
        from signals.listener import backoff_delay

        with pytest.raises(ValueError):
            backoff_delay(-1)


class TestHeartbeatContract:
    def test_interval_constant(self) -> None:
        from signals.listener import HEARTBEAT_INTERVAL_S

        assert HEARTBEAT_INTERVAL_S == 30.0

    def test_ping_pong_models_match_spec(self) -> None:
        from signals.protocol import SystemPing, SystemPong, parse_wire_event

        parsed = parse_wire_event({"event": "SYSTEM_PING"})
        assert isinstance(parsed, SystemPing)
        parsed = parse_wire_event({"event": "SYSTEM_PONG", "broker_connected": True})
        assert isinstance(parsed, SystemPong) and parsed.broker_connected
        assert parse_wire_event({"event": "MYSTERY"}) is None
        assert parse_wire_event("not json at all") is None
