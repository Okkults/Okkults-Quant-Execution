"""Mock run harness — the full terminal cycle with zero credentials.

``python main.py --mock`` drives a fake paper account through:

1. a mock BATCH_SIGNAL with 12 tickers (sized against $50,000 equity)
2. concurrent execution + disaster stops (payload-driven)
3. a server-style EXIT_SIGNAL at the open cross, then the PnL summary

This exists so anyone can prove the client works end-to-end before
minting an Okkults license or creating Alpaca paper keys.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from rich.console import Console

from brokers.mock import MOCK_EXAMPLE_TICKERS, MockBroker
from config.settings import Settings
from core.executor import SignalExecutor
from signals.listener import ListenerCallbacks, SignalListener
from signals.protocol import ET, BatchSignal, ExitSignal

console = Console()


def build_mock_batch(now: datetime | None = None) -> BatchSignal:
    """A 12-setup example payload, structurally identical to live signals."""
    now = now or datetime.now(tz=ET)
    cutoff = (now + timedelta(minutes=8)).isoformat()
    signals = [
        {
            "signal_id": f"sig-{i:02d}-{ticker}",
            "ticker": ticker,
            "side": "BUY",
            "reference_price": 100.0 + (i % 5) * 7.5,
            "limit_price": None,
            "entry_cutoff_at": cutoff,
            "exit_rules": {
                "disaster_stop_pct": 0.07,
                "profit_target_pct": None,
                "trailing_stop_pct": None,
            },
            "batch_id": "MOCK-BATCH-001",
        }
        for i, ticker in enumerate(MOCK_EXAMPLE_TICKERS)
    ]
    return BatchSignal.model_validate(
        {"event": "BATCH_SIGNAL", "batch_id": "MOCK-BATCH-001", "signals": signals}
    )


class CaptureListener(SignalListener):
    """Real-listener interface that records upstream telemetry in memory."""

    def __init__(self) -> None:
        super().__init__(
            ws_url="wss://mock.invalid/v1/stream",
            license_key="ok_paper_mock",
            callbacks=ListenerCallbacks(
                on_batch=self._ignore,
                on_exit=self._ignore,
                on_close_position=self._ignore,
            ),
        )
        self.captured: list[dict] = []

    async def send(self, payload: dict) -> bool:
        self.captured.append(payload)
        return True

    @staticmethod
    async def _ignore(_: object) -> None:
        return None


class MockSignalInjector:
    """Doubles as the listener for telemetry capture during the mock cycle."""

    def __init__(self) -> None:
        self._listener = CaptureListener()

    @property
    def listener(self) -> SignalListener:
        return self._listener

    @property
    def telemetry(self) -> list[dict]:
        return self._listener.captured

    async def run_cycle(
        self, settings: Settings, executor: SignalExecutor | None = None
    ) -> None:
        broker = MockBroker(equity=50_000.0)
        if executor is None:
            executor = SignalExecutor(
                broker=broker, listener=self._listener, settings=settings
            )
        console.rule("[bold magenta]MOCK CYCLE — 12-ticker batch, no real money")
        batch = build_mock_batch()
        await executor.handle_batch_signal(batch)
        await asyncio.sleep(0)
        console.rule("[bold magenta]MOCK CYCLE — server EXIT_SIGNAL (open cross)")
        await executor.handle_exit_signal(
            ExitSignal(
                event="EXIT_SIGNAL",
                action="EXIT_ALL",
                reason="POST_EARNINGS_OPEN_CROSS",
            )
        )
        console.print(
            f"[bold green]Mock cycle complete — {len(self.telemetry)} fill "
            "confirmations captured, zero keys required.[/bold green]"
        )
