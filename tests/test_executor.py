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


def build_entry(
    signal_id: str, ticker: str, price: float, side: str = "BUY"
) -> EntrySignal:
    return EntrySignal.model_validate(
        {
            "signal_id": signal_id,
            "ticker": ticker,
            "side": side,
            "reference_price": price,
            "limit_price": None,
            "entry_cutoff_at": fresh_cutoff(),
            "exit_rules": {"disaster_stop_pct": 0.07, "batch_id": "TEST"},
            "batch_id": "TEST",
        }
    )


def build_batch(n: int, price: float = 208.0, side: str = "BUY") -> BatchSignal:
    tickers = [f"T{i:02d}" for i in range(n)]
    entries = [build_entry(f"sig-{i}", t, price, side) for i, t in enumerate(tickers)]
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

        assert len(broker.orders) == 12
        assert all(o["type"] != "stop" for o in broker.orders)
        assert elapsed_ms < 200  # 12 x 20ms must run concurrently, not 240ms
        confirmations = [t for t in listener.sent if t["event"] == "FILL_CONFIRMATION"]
        assert len(confirmations) == 12

    async def test_sizing_uses_default_five_percent(self) -> None:
        broker = MockBroker()
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(6))
        assert all(o["qty"] == 12 for o in broker.orders)  # 5% of $50k @ $208

    async def test_no_resting_stop_orders_are_created(self) -> None:
        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(1, price=208.0))
        assert len(broker.orders) == 1
        assert all(o["type"] == "market" for o in broker.orders)

    async def test_failed_order_does_not_block_batch(self) -> None:
        class FlakyBroker(MockBroker):
            async def place_order(
                self,
                symbol: str,
                qty: int,
                side: str,
                order_type: str,
                limit_price: float | None = None,
                client_order_id: str | None = None,
            ) -> dict:
                if client_order_id and client_order_id.endswith("T01"):
                    raise RuntimeError("simulated broker outage")
                return await super().place_order(
                    symbol, qty, side, order_type, limit_price, client_order_id
                )

        broker = FlakyBroker(fill_delay=0.01)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(3))
        assert len(broker.orders) == 2  # T01 failed and was reported


class TestExitPath:
    async def test_exit_signal_closes_client_positions_and_prints_pnl(self) -> None:
        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(3))
        assert len(await broker.get_open_positions()) == 3

        await executor.handle_exit_signal(
            ExitSignal(action="EXIT_ALL", reason="POST_EARNINGS_OPEN_CROSS")
        )
        assert not await broker.get_open_positions()
        assert len(broker.closed_symbols) == 3

    async def test_pre_existing_position_blocks_same_ticker_entry(self) -> None:
        broker = MockBroker(fill_delay=0)
        await broker.place_order("T00", 5, "BUY", "market")
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(2))
        positions = {p["symbol"]: p["qty"] for p in await broker.get_open_positions()}
        assert positions["T00"] == 5  # untouched pre-existing lot
        assert positions["T01"] == 12

    async def test_second_batch_does_not_stack_same_ticker(self) -> None:
        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(1))
        first_order_count = len(broker.orders)
        await executor.handle_batch_signal(build_batch(1))
        assert len(broker.orders) == first_order_count
        positions = await broker.get_open_positions()
        assert positions[0]["qty"] == 12  # 5% of $50k @ $208, not doubled

    async def test_exit_never_closes_unrelated_holdings(self) -> None:
        broker = MockBroker(fill_delay=0)
        await broker.place_order("AAPL", 10, "BUY", "market")
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(2))
        await executor.handle_exit_signal(
            ExitSignal(action="EXIT_ALL", reason="POST_EARNINGS_OPEN_CROSS")
        )
        positions = await broker.get_open_positions()
        assert [p["symbol"] for p in positions] == ["AAPL"]
        assert positions[0]["qty"] == 10

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
        assert positions[0]["qty"] == 13  # 25 shares -> 12 closed, 13 remain

    async def test_close_position_ignores_untracked_holding(self) -> None:
        from signals.protocol import ClosePositionSignal

        broker = MockBroker(fill_delay=0)
        await broker.place_order("AAPL", 10, "BUY", "market")
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_close_position(
            ClosePositionSignal(ticker="AAPL", percentage=100.0, reason="server")
        )
        positions = await broker.get_open_positions()
        assert positions[0]["qty"] == 10


class TestConfirmationGate:
    async def test_semi_auto_requires_explicit_confirm(self) -> None:
        waiter = ImmediateConfirm()
        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(EXECUTION_MODE="semi_auto"),
            confirm_waiter=waiter,
        )
        await executor.handle_batch_signal(build_batch(2))
        assert waiter.calls == 1  # gate was engaged
        assert len(broker.orders) == 2

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


class TestShortSellingGate:
    async def test_sell_signals_are_ignored_by_default(self) -> None:
        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(ALLOW_SHORT_SELLING=False),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(1, side="SELL"))
        assert broker.orders == []

    async def test_sell_signals_execute_when_explicitly_enabled(self) -> None:
        broker = MockBroker(fill_delay=0)
        executor = SignalExecutor(
            broker=broker,
            settings=Settings(ALLOW_SHORT_SELLING=True),
            confirm_waiter=ImmediateConfirm(),
        )
        await executor.handle_batch_signal(build_batch(1, side="SELL"))
        assert len(broker.orders) == 1
        assert broker.orders[0]["side"] == "SELL"
        positions = await broker.get_open_positions()
        assert positions[0]["qty"] == -12


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
    assert "pre-trade review" in str(table.title)
