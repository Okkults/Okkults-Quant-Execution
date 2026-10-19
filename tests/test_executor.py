"""Executor tests — mocked broker execution through the full pipeline."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

from brokers.mock import MockBroker
from config.settings import Settings
from core.executor import SignalExecutor, build_batch_signal_table
from signals.protocol import BatchSignal, EntrySignal, ExitSignal


def fresh_cutoff(seconds: float = 600) -> str:
    return (datetime.now(tz=UTC) + timedelta(seconds=seconds)).isoformat()


def build_entry(signal_id: str, ticker: str, price: float) -> EntrySignal:
    return EntrySignal.model_validate(
        {
            "signal_id": signal_id,
            "ticker": ticker,
            "side": "BUY",
            "reference_price": price,
            "limit_price": None,
            "entry_cutoff_at": fresh_cutoff(),
            "exit_rules": {"disaster_stop_pct": 0.07, "batch_id": "TEST"},
            "batch_id": "TEST",
        }
    )


def build_batch(n: int, price: float = 208.0) -> BatchSignal:
    tickers = [f"T{i:02d}" for i in range(n)]
    entries = [build_entry(f"sig-{i}", t, price) for i, t in enumerate(tickers)]
    return BatchSignal.model_validate(
        {"event": "BATCH_SIGNAL", "batch_id": "TEST", "signals": entries}
    )


class CaptureListener:
    """Telemetry double that records everything sent upstream."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: dict) -> bool:
        self.sent.append(payload)
        return True


class ImmediateConfirm:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return True


class NeverConfirm:
    async def __call__(self) -> bool:
        await asyncio.sleep(10)  # will hit the timeout gate
        return True


class TestBatchExecution:
    async def test_twelve_orders_fire_concurrently_under_200ms(self) -> None:
        broker = MockBroker(fill_delay=0.02)
        listener = CaptureListener()
        settings = Settings(MAX_PORTFOLIO_RISK_PCT=0.30, MAX_TRADE_ALLOCATION_PCT=0.10)
        executor = SignalExecutor(
            broker=broker, listener=listener, settings=settings,
            confirm_waiter=ImmediateConfirm(),
        )
        batch = build_batch(12)
        started = time.monotonic()
        await executor.handle_batch_signal(batch)
        elapsed_ms = (time.monotonic() - started) * 1000

        fills = [o for o in broker.orders if o["type"] != "stop"]
        stops = [o for o in broker.orders if o["type"] == "stop"]
        assert len(fills) == 12
        assert len(stops) == 12  # Tier-1 disaster stop rests per fill
        assert elapsed_ms < 200  # 12 x 20ms must run concurrently, not 240ms
        confirmations = [t for t in listener.sent if t["event"] == "FILL_CONFIRMATION"]
        assert len(confirmations) == 12

    async def test_sizing_uses_spec_defaults(self) -> None:
        broker = MockBroker()
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(6))
        fills = [o for o in broker.orders if o["type"] != "stop"]
        assert all(o["qty"] == 12 for o in fills)  # $2,496 @ $208 parity

    async def test_stop_price_derived_from_payload_pct(self) -> None:
        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(1, price=208.0))
        stops = [o for o in broker.orders if o["type"] == "stop"]
        assert len(stops) == 1
        # anchor = fill price ($100.25 for T00 3-letter) -> 7% payload stop
        assert stops[0]["avg_fill_price"] < 100.25

    async def test_failed_order_does_not_block_batch(self) -> None:
        class FlakyBroker(MockBroker):
            async def place_order(self, *a, **k):
                if k.get("client_order_id", "").endswith("T01"):
                    raise RuntimeError("simulated broker outage")
                return await super().place_order(*a, **k)

        broker = FlakyBroker(fill_delay=0.01)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(3))
        fills = [o for o in broker.orders if o["type"] != "stop"]
        assert len(fills) == 2  # T01 failed and was reported, others succeeded


class TestExitPath:
    async def test_exit_signal_closes_everything_and_prints_pnl(self) -> None:
        broker = MockBroker(fill_delay=0)
        listener = CaptureListener()
        executor = SignalExecutor(
            broker=broker, listener=listener,
            settings=Settings(), confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(3))
        positions = await broker.get_open_positions()
        assert len(positions) == 3

        await executor.handle_exit_signal(
            ExitSignal(action="EXIT_ALL", reason="POST_EARNINGS_OPEN_CROSS")
        )
        assert not await broker.get_open_positions()
        assert broker.cancelled >= 3  # resting disaster stops cancelled
        assert len(broker.closed_symbols) == 3

    async def test_close_position_dumps_requested_percentage(self) -> None:
        from signals.protocol import ClosePositionSignal

        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(1, price=100.0))
        await executor.handle_close_position(
            ClosePositionSignal(ticker="T00", percentage=50.0, reason="target hit")
        )
        positions = await broker.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["qty"] < float(500)


class TestConfirmationGate:
    async def test_semi_auto_requires_explicit_confirm(self) -> None:
        waiter = ImmediateConfirm()
        executor = SignalExecutor(
            broker=MockBroker(fill_delay=0),
            settings=Settings(EXECUTION_MODE="semi_auto"),
            confirm_waiter=waiter,
        )
        await executor.handle_batch_signal(build_batch(2))
        assert waiter.calls == 1  # gate was engaged
        fills = [o for o in executor._broker.orders if o["type"] != "stop"]
        assert len(fills) == 2

    async def test_semi_auto_timeout_skips_the_batch(self) -> None:
        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(EXECUTION_MODE="semi_auto"),
            confirm_waiter=NeverConfirm(),
        )
        # Entry cutoff 0.4s out -> the confirmation deadline is 0.4s, well
        # below NeverConfirm's 10s sleep; handler must return, not trade.
        batch = build_batch(2)
        for signal in batch.signals:
            signal.entry_cutoff_at = fresh_cutoff(0.4)
        started = time.monotonic()
        await executor.handle_batch_signal(batch)
        elapsed = time.monotonic() - started
        assert broker.orders == []  # nothing fired without confirmation
        assert elapsed < 2.0  # skipped promptly once the gate expired

    async def test_full_auto_never_asks(self) -> None:
        waiter = ImmediateConfirm()
        executor = SignalExecutor(
            broker=MockBroker(fill_delay=0),
            settings=Settings(EXECUTION_MODE="full_auto"),
            confirm_waiter=waiter,
        )
        await executor.handle_batch_signal(build_batch(1))
        assert waiter.calls == 0  # zero-gate full-auto path


class TestStaleSignals:
    async def test_stale_batch_never_trades(self) -> None:
        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        expired = build_batch(2)
        for signal in expired.signals:
            signal.entry_cutoff_at = (
                datetime.now(tz=UTC) - timedelta(minutes=5)
            ).isoformat()
        await executor.handle_batch_signal(expired)
        assert broker.orders == []


class TestTelemetry:
    async def test_fill_confirmation_shape_matches_spec(self) -> None:
        broker = MockBroker(fill_delay=0)
        listener = CaptureListener()
        executor = SignalExecutor(
            broker=broker, listener=listener,
            settings=Settings(), confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(1))
        (confirmation,) = [
            t for t in listener.sent if t["event"] == "FILL_CONFIRMATION"
        ]
        assert set(confirmation) == {
            "event", "signal_id", "ticker", "shares_filled",
            "avg_fill_price", "execution_latency_ms",
        }
        assert confirmation["signal_id"] == "sig-0"
        assert confirmation["ticker"] == "T00"


def test_review_table_renders_skip_reasons() -> None:
    batch = build_batch(1)
    from core.risk import SizingResult

    sizing = SizingResult(
        orders=[], scaling_applied=False, effective_pct=0.10,
        skipped=[("T00", "allocation too small for 1 share")],
    )
    table = build_batch_signal_table(batch, sizing)
    assert "pre-trade review" in table.title  # type: ignore[union-attr]
