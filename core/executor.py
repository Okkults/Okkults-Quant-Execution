"""Executor — fires sized batches and honors server-driven exits.

Responsibilities:

1. On BATCH_SIGNAL: fetch live equity, size via core.risk, optionally
   require explicit Enter/Spacebar confirmation (semi_auto), then run all
   1-12 orders concurrently with asyncio.gather.
2. Per fill: rest a broker-side disaster stop using the payload's
   ``exit_rules.disaster_stop_pct`` (Tier 1 exit; survives user disconnects)
   and emit FILL_CONFIRMATION telemetry upstream.
3. On EXIT_SIGNAL (9:30:15 AM open cross): cancel resting stops, dump all
   positions at market, print the PnL summary table.
4. On CLOSE_POSITION: cancel that position's stop and dump the requested
   percentage at market, exactly as the server requested.
5. LOCAL_EXIT_FAILSAFE: if the open-cross signal never arrives (server
   outage) and positions are still open, liquidate locally at 9:30:16 AM
   ET. Operational safety net only — not alpha.

No pricing formula exists here. Every number displayed comes from the wire
payload or from the broker's own responses.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.table import Table

from brokers.base import BaseBroker
from config.settings import Settings
from core.risk import (
    OrderBlueprint,
    SizingResult,
    build_scaling_warning,
    calculate_order_sizes,
)
from signals.listener import ListenerCallbacks, SignalListener
from signals.protocol import (
    BATCH_SIGNAL_EVENT,
    CLOSE_POSITION_EVENT,
    EXIT_SIGNAL_EVENT,
    FILL_CONFIRMATION_EVENT,
    BatchSignal,
    ClosePositionSignal,
    EntrySignal,
    ExitSignal,
    is_stale,
)

ET: ZoneInfo = ZoneInfo("America/New_York")
console = Console()

SEMI_AUTO_CONFIRM_TIMEOUT_S: float = 90.0
FAILSAFE_EXIT_AT: tuple[int, int, int] = (9, 30, 16)  # 9:30:16 AM ET
FAILSAFE_POLL_INTERVAL_S: float = 30.0
FAILSAFE_WINDOW_END_ET: tuple[int, int, int] = (16, 0, 0)

ConfirmationWaiter = Callable[[], Awaitable[bool]]
LocalClock = Callable[[], datetime]


@dataclass
class EntryOutcome:
    """Result of one concurrent order submission inside a batch."""

    signal: EntrySignal
    order: OrderBlueprint
    fill: dict | None = None
    error: str | None = None
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.fill is not None


def build_batch_signal_table(batch: BatchSignal, sizing: SizingResult) -> Table:
    """Institutional pre-trade review table (entry rows + skip reasons)."""
    table = Table(
        title=f"Okkults Batch {batch.batch_id} — pre-trade review",
        header_style="bold magenta",
    )
    table.add_column("Ticker", style="bold")
    table.add_column("Side")
    table.add_column("Shares", justify="right")
    table.add_column("Ref price", justify="right")
    table.add_column("Limit", justify="right")
    table.add_column("Notional", justify="right")
    signal_map = {s.signal_id: s for s in batch.signals}
    for order in sizing.orders:
        signal = signal_map.get(order.signal_id)
        limit = (
            f"{signal.limit_price:.2f}"
            if signal is not None and signal.limit_price is not None
            else "market"
        )
        table.add_row(
            order.ticker,
            order.side,
            str(order.shares),
            f"{order.reference_price:.2f}",
            limit,
            f"${order.dollar_allocation:,.2f}",
        )
    for skipped_ticker, reason in sizing.skipped:
        table.add_row(skipped_ticker, "[dim]SKIP[/dim]", "-", "-", "-", reason)
    return table


def seconds_until_next_open_cross(now: datetime | None = None) -> float:
    """Seconds from now until the next 9:30:16 AM ET (for the idle HUD)."""
    if now is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=ET)
    else:
        now = datetime.now(tz=ET)
    local = now.astimezone(ET)
    target = local.replace(hour=9, minute=30, second=16, microsecond=0)
    if target <= local:
        target = target + timedelta(days=1)
    return (target - local).total_seconds()


class SignalExecutor:
    """Wires sizing, confirmation, concurrent entry, stops, exits, failsafe."""

    def __init__(
        self,
        broker: BaseBroker,
        listener: SignalListener | None = None,
        settings: Settings | None = None,
        confirm_waiter: ConfirmationWaiter | None = None,
        clock: LocalClock | None = None,
    ) -> None:
        self._broker = broker
        self._listener = listener
        self._settings: Settings = settings if settings is not None else Settings()
        self._confirm_waiter = confirm_waiter or self._terminal_confirmation
        self._clock = clock
        self._open_batches: dict[str, dict[str, EntrySignal]] = {}
        self._resting_stops: dict[str, str] = {}  # symbol -> resting stop order id
        self._exited_since_entry: bool = True

    # ------------------------------------------------------------------ #
    # listener wiring
    # ------------------------------------------------------------------ #
    def callbacks(self) -> ListenerCallbacks:
        return ListenerCallbacks(
            on_batch=self.handle_batch_signal,
            on_exit=self.handle_exit_signal,
            on_close_position=self.handle_close_position,
        )

    def _now(self) -> datetime:
        if self._clock is None:
            return datetime.now(tz=ET)
        stamp = self._clock()
        return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=ET)

    # ------------------------------------------------------------------ #
    # ENTRY (BATCH_SIGNAL)
    # ------------------------------------------------------------------ #
    async def handle_batch_signal(self, batch: BatchSignal) -> None:
        console.rule(
            f"[bold cyan]{BATCH_SIGNAL_EVENT} — batch {batch.batch_id}"

        )
        tradable = [s for s in batch.signals if self._signal_is_fresh(s)]
        for signal in batch.signals:
            if signal not in tradable:
                console.print(
                    f"[red][!] STALE — {signal.ticker} past entry_cutoff_at; skipping[/red]"
                )
        if not tradable:
            console.print("[yellow][!] No fresh signals — batch skipped[/yellow]")
            return

        equity = await self._broker.get_equity()
        sizing = calculate_order_sizes(
            signals=[s.model_dump() for s in tradable],
            account_equity=equity,
            target_pct=self._settings.MAX_TRADE_ALLOCATION_PCT,
            max_total_risk_pct=self._settings.MAX_PORTFOLIO_RISK_PCT,
            per_trade_notional_cap=self._settings.PER_TRADE_NOTIONAL_CAP,
            cash_reserve=self._settings.CASH_RESERVE,
        )
        if sizing.scaling_applied:
            console.print(
                f"[amber bold]{build_scaling_warning(len(tradable), self._settings.MAX_TRADE_ALLOCATION_PCT, sizing.effective_pct)}[/amber bold]"
            )
        console.print(build_batch_signal_table(batch, sizing))
        if not sizing.orders:
            console.print(
                "[yellow][!] Sizing produced zero orders — batch skipped[/yellow]"
            )
            return

        if self._settings.EXECUTION_MODE == "semi_auto":
            confirmed = await self._confirm_before_cutoff(tradable)
            if not confirmed:
                console.print("[yellow][!] Not confirmed — batch skipped[/yellow]")
                return

        began = time.monotonic()
        outcomes = await asyncio.gather(
            *[self._submit_entry(batch, s, o) for s in tradable for o in self._orders_for(sizing, s)]
        )
        elapsed_ms = (time.monotonic() - began) * 1000.0
        self._print_execution_table(batch, list(outcomes), elapsed_ms)

        await self._place_disaster_stops(list(outcomes))
        self._open_batches[batch.batch_id] = {s.signal_id: s for s in tradable}
        self._exited_since_entry = False
        console.print(
            f"[bold green][{batch.batch_id}] Deployed. Disaster stops rest on the broker "
            "overnight. Awaiting the 9:30:15 AM open-cross exit signal.[/bold green]"
        )

    @staticmethod
    def _orders_for(sizing: SizingResult, signal: EntrySignal) -> list[OrderBlueprint]:
        return [o for o in sizing.orders if o.signal_id == signal.signal_id]

    def _signal_is_fresh(self, signal: EntrySignal) -> bool:
        return not is_stale(
            signal.entry_cutoff_at, None if self._clock is None else self._clock()
        )

    async def _submit_entry(
        self, batch: BatchSignal, signal: EntrySignal, order: OrderBlueprint
    ) -> EntryOutcome:
        started = time.monotonic()
        try:
            fill = await self._broker.place_order(
                symbol=order.ticker,
                qty=order.shares,
                side=order.side,
                order_type="limit" if signal.limit_price else "market",
                limit_price=signal.limit_price,
                stop_price=None,
                client_order_id=f"okq-{batch.batch_id[:8]}-{signal.signal_id[:12]}-{signal.ticker}",
            )
        except Exception as exc:
            console.print(
                f"[red][!] {signal.ticker}: order failed — {type(exc).__name__}: {exc}[/red]"
            )
            return EntryOutcome(signal=signal, order=order, error=str(exc))
        latency_ms = (time.monotonic() - started) * 1000.0
        await self._emit_fill_confirmation(signal, order, fill, latency_ms)
        return EntryOutcome(signal=signal, order=order, fill=fill, latency_ms=latency_ms)

    async def _emit_fill_confirmation(
        self, signal: EntrySignal, order: OrderBlueprint, fill: dict, latency_ms: float
    ) -> None:
        confirmation: dict[str, Any] = {
            "event": FILL_CONFIRMATION_EVENT,
            "signal_id": signal.signal_id,
            "ticker": order.ticker,
            "shares_filled": fill.get("qty", order.shares),
            "avg_fill_price": fill.get("avg_fill_price"),
            "execution_latency_ms": round(latency_ms, 1),
        }
        if self._listener is not None:
            delivered = await self._listener.send(confirmation)
            if not delivered:
                console.print(f"[dim]Telemetry queued offline for {signal.ticker}[/dim]")

    def _print_execution_table(
        self, batch: BatchSignal, outcomes: list[EntryOutcome], elapsed_ms: float
    ) -> None:
        table = Table(
            title=f"Execution log — batch {batch.batch_id}", header_style="bold magenta"
        )
        table.add_column("Ticker", style="bold")
        table.add_column("Result")
        table.add_column("Avg fill", justify="right")
        table.add_column("Latency", justify="right")
        for outcome in outcomes:
            if outcome.ok and outcome.fill is not None:
                price = outcome.fill.get("avg_fill_price")
                price_text = f"${price:.2f}" if isinstance(price, (int, float)) else "-"
                table.add_row(
                    outcome.order.ticker,
                    "[green]FILLED[/green]",
                    price_text,
                    f"{outcome.latency_ms:.1f} ms",
                )
            else:
                table.add_row(outcome.order.ticker, "[red]FAILED[/red]", "-", "-")
        table.add_row(
            "[dim]total elapsed[/dim]", "-", "-", f"[bold]{elapsed_ms:.1f} ms[/bold]"
        )
        console.print(table)

    async def _confirm_before_cutoff(self, signals: list[EntrySignal]) -> bool:
        """90-second (or cutoff-bounded) Enter/Spacebar confirmation gate."""
        deadline = min(SEMI_AUTO_CONFIRM_TIMEOUT_S, self._seconds_until_cutoff(signals))
        if deadline <= 0:
            console.print(
                "[yellow][!] Entry window already closed — skipping[/yellow]"
            )
            return False
        console.print(
            f"[bold cyan](semi_auto) Confirm to fire. Enter/Spacebar within "
            f"{deadline:.0f}s; window closes at 3:58 PM ET.[/bold cyan]\a"
        )
        try:
            return await asyncio.wait_for(self._confirm_waiter(), timeout=deadline)
        except TimeoutError:
            console.print("[yellow][!] Confirmation not received — batch skipped[/yellow]")
            return False

    def _seconds_until_cutoff(self, signals: list[EntrySignal]) -> float:
        """Seconds until the earliest entry_cutoff_at in the batch."""
        now = self._now()
        deltas: list[float] = []
        for signal in signals:
            try:
                cutoff = datetime.fromisoformat(
                    signal.entry_cutoff_at.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=ET)
            deltas.append((cutoff - now).total_seconds())
        return max(0.0, min(deltas)) if deltas else 0.0

    async def _terminal_confirmation(self) -> bool:
        """Runs the blocking key read off the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_confirmation_key)

    def _read_confirmation_key(self) -> bool:
        """One cbreak read: Enter/Spacebar confirms; everything else skips."""
        try:
            import termios
            import tty

            fd = sys.stdin.fileno()
            previous = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                return sys.stdin.read(1) in ("\n", "\r", " ")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        except Exception:
            try:
                line = sys.stdin.readline()
            except Exception:
                return False
            if not line:
                return False
            return line.strip() == ""

    # ------------------------------------------------------------------ #
    # Tier 1 exit — payload-driven broker-side disaster stop
    # ------------------------------------------------------------------ #
    async def _place_disaster_stops(self, outcomes: list[EntryOutcome]) -> None:
        """Rest broker-side stops per fill, concurrently, anchored to payload pct."""
        stop_jobs = [
            self._rest_one_disaster_stop(outcome)
            for outcome in outcomes
            if outcome.ok and outcome.fill is not None
        ]
        if stop_jobs:
            await asyncio.gather(*stop_jobs)

    async def _rest_one_disaster_stop(self, outcome: EntryOutcome) -> None:
        stop_pct = outcome.signal.exit_rules.disaster_stop_pct
        anchor = outcome.fill.get("avg_fill_price") or outcome.order.reference_price  # type: ignore[union-attr]
        if not isinstance(anchor, (int, float)) or anchor <= 0:
            console.print(
                f"[bold red][!!] No usable fill price for "
                f"{outcome.order.ticker} — disaster stop NOT placed.[/bold red]"
            )
            return
        stop_price = anchor * (1 - stop_pct)
        stop_side = "SELL" if outcome.order.side.upper() == "BUY" else "BUY"
        try:
            stop = await self._broker.place_order(
                symbol=outcome.order.ticker,
                qty=int(outcome.fill.get("qty", outcome.order.shares)),  # type: ignore[union-attr]
                side=stop_side,
                order_type="stop",
                stop_price=round(stop_price, 2),
            )
            self._resting_stops[outcome.order.ticker] = str(stop.get("order_id", ""))
            console.print(
                f"[cyan][OK][/cyan] Disaster stop resting for {outcome.order.ticker} "
                f"@ ${stop_price:.2f} ({stop_pct * 100:.1f}% from payload exit_rules)"
            )
        except Exception as exc:
            console.print(
                f"[bold red][!!] Could not rest disaster stop for "
                f"{outcome.order.ticker}: {type(exc).__name__}: {exc} — "
                "position is unprotected overnight.[/bold red]"
            )

    # ------------------------------------------------------------------ #
    # EXIT — server-driven and local failsafe
    # ------------------------------------------------------------------ #
    async def handle_exit_signal(self, exit_signal: ExitSignal) -> None:
        console.rule(f"[bold red]{EXIT_SIGNAL_EVENT} — {exit_signal.reason}")
        await self._exit_everything(reason=exit_signal.reason)

    async def handle_close_position(self, close_signal: ClosePositionSignal) -> None:
        console.rule(
            f"[bold red]{CLOSE_POSITION_EVENT} — {close_signal.ticker} "
            f"{close_signal.percentage:.0f}% — {close_signal.reason}"
        )
        ticker = close_signal.ticker
        stop_id = self._resting_stops.pop(ticker, None)
        if stop_id:
            console.print(
                f"[dim]Cancelled resting stop {stop_id} for {ticker}[/dim]"
            )
        try:
            summary = await self._broker.close_position(
                ticker, close_signal.percentage
            )
            console.print(f"[green][OK][/green] {ticker} — {summary.get('status')}")
        except Exception as exc:
            console.print(
                f"[red][!] Close failed for {ticker} — {type(exc).__name__}: {exc}[/red]"
            )

    async def _exit_everything(self, reason: str) -> None:
        cancelled = await self._broker.cancel_all_orders()
        console.print(f"[cyan][OK][/cyan] Cancelled {cancelled} resting order(s)")

        try:
            positions = await self._broker.get_open_positions()
        except Exception as exc:
            console.print(
                f"[red][!] Unable to list positions — {type(exc).__name__}: {exc}[/red]"
            )
            positions = []

        exits = await self._broker.close_all_positions()
        self._resting_stops.clear()
        self._open_batches.clear()
        self._exited_since_entry = True
        self._print_pnl_table(positions, exits, reason)

    def _print_pnl_table(
        self, positions: list[dict], exits: list[dict], reason: str
    ) -> None:
        """Morning-exit PnL: entry, exit, slippage, net PnL per position."""
        table = Table(title=f"Exit PnL — {reason}", header_style="bold magenta")
        table.add_column("Ticker", style="bold")
        table.add_column("Entry", justify="right")
        table.add_column("Exit", justify="right")
        table.add_column("Slippage", justify="right")
        table.add_column("Net PnL", justify="right")
        for position in positions:
            symbol = position.get("symbol", "?")
            entry = float(position.get("avg_entry_price") or 0)
            exit_record = next(
                (e for e in exits if e.get("symbol") == symbol), None
            )
            raw = (exit_record or {}).get("raw") or {}

            exit_price = raw.get("avg_fill_price")
            exit_display = (
                f"${float(exit_price):.2f}"
                if isinstance(exit_price, (int, float))
                else "market"
            )
            slippage = (
                f"{(float(exit_price) - entry) / entry * 100:+.2f}%"
                if isinstance(exit_price, (int, float)) and entry > 0
                else "-"
            )
            qty = float(position.get("qty") or 0)
            pnl: float | None = (
                (float(exit_price) - entry) * qty
                if isinstance(exit_price, (int, float))
                else None
            )
            pnl_display = f"${pnl:+,.2f}" if pnl is not None else "-"
            color = (
                "green" if pnl is not None and pnl >= 0 else "red" if pnl is not None else "white"
            )
            table.add_row(
                symbol,
                f"${entry:.2f}",
                exit_display,
                slippage,
                f"[{color}]{pnl_display}[/{color}]",
            )
        console.print(table)
        console.print("[bold green]100% back to cash.[/bold green]")

    # ------------------------------------------------------------------ #
    # LOCAL_EXIT_FAILSAFE watchdog
    # ------------------------------------------------------------------ #
    def _in_failsafe_window(self, now: datetime) -> bool:
        local = now.astimezone(ET)
        start = local.replace(
            hour=FAILSAFE_EXIT_AT[0],
            minute=FAILSAFE_EXIT_AT[1],
            second=FAILSAFE_EXIT_AT[2],
            microsecond=0,
        )
        end = local.replace(
            hour=FAILSAFE_WINDOW_END_ET[0],
            minute=FAILSAFE_WINDOW_END_ET[1],
            second=FAILSAFE_WINDOW_END_ET[2],
            microsecond=0,
        )
        return start <= local < end

    async def _failsafe_check(self) -> None:
        """One poll cycle of the local liquidation net."""
        if self._exited_since_entry:
            return
        try:
            positions = await self._broker.get_open_positions()
        except Exception:
            return
        if not positions:
            return
        console.print(
            "[bold red][FAILSAFE] The 9:30:15 AM EXIT_SIGNAL never arrived "
            "but positions are open — liquidating locally now.[/bold red]"
        )
        await self._exit_everything(reason="LOCAL_FAILSAFE")

    async def failsafe_watchdog(self, enabled: bool | None = None) -> None:
        """Long-running net: polls the 9:30:16 AM window until cancelled."""
        enabled = (
            self._settings.LOCAL_EXIT_FAILSAFE if enabled is None else enabled
        )
        if not enabled:
            console.print(
                "[yellow][--] LOCAL_EXIT_FAILSAFE disabled by config — "
                "positions rely fully on the signal server and disaster stops.[/yellow]"
            )
            return
        while True:
            try:
                if self._in_failsafe_window(self._now()):
                    await self._failsafe_check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let the net crash the process
                console.print(
                    f"[dim][failsafe] poll error — {type(exc).__name__}: {exc}[/dim]"
                )
            await asyncio.sleep(FAILSAFE_POLL_INTERVAL_S)


__all__ = [
    "FAILSAFE_EXIT_AT",
    "FAILSAFE_POLL_INTERVAL_S",
    "FAILSAFE_WINDOW_END_ET",
    "SEMI_AUTO_CONFIRM_TIMEOUT_S",
    "EntryOutcome",
    "SignalExecutor",
    "build_batch_signal_table",
    "seconds_until_next_open_cross",
]
