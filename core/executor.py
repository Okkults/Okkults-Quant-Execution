"""Executor — fires sized batches and honors server-driven exits.

Responsibilities:

1. On BATCH_SIGNAL: fetch live equity, size via core.risk, optionally
   require explicit Enter/Spacebar confirmation (semi_auto), then run all
   1-12 orders concurrently with asyncio.gather.
2. Track only the positions this client opened and emit FILL_CONFIRMATION
   telemetry upstream. No resting stop orders are ever placed.
3. On EXIT_SIGNAL (9:30:15 AM open cross): liquidate only client-opened
   positions at market, print the PnL summary table.
4. On CLOSE_POSITION: close the requested percentage of the client-opened
   position, exactly as the server requested.
5. LOCAL_EXIT_FAILSAFE: if the open-cross signal never arrives (server
   outage) and client-opened positions are still open, liquidate them
   locally at 9:30:16 AM ET. Operational safety net only — not alpha.

Unrelated holdings already in the broker account are never resized or
liquidated by this executor; entries for occupied symbols are skipped.

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
from typing import Any, Protocol
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
from signals.listener import ListenerCallbacks
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


class TelemetrySink(Protocol):
    """Minimal listener surface the executor needs for fill confirmations."""

    async def send(self, payload: dict) -> bool: ...


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
    """Wires sizing, confirmation, concurrent entry, exits, and failsafe."""

    def __init__(
        self,
        broker: BaseBroker,
        listener: TelemetrySink | None = None,
        settings: Settings | None = None,
        confirm_waiter: ConfirmationWaiter | None = None,
        clock: LocalClock | None = None,
    ) -> None:
        self._broker = broker
        self._listener = listener
        self._settings: Settings = settings if settings is not None else Settings()
        self._using_terminal_confirm = confirm_waiter is None
        self._confirm_waiter = confirm_waiter or self._terminal_confirmation
        self._clock = clock
        self._open_batches: dict[str, dict[str, EntrySignal]] = {}
        # Signed quantities opened by this process. Unrelated broker holdings
        # never enter this map, so they can never be closed by an exit.
        self._client_positions: dict[str, float] = {}
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
        try:
            existing_positions = await self._broker.get_open_positions()
        except Exception as exc:  # cannot prove safety -> refuse to trade
            console.print(
                f"[bold red]✗ Cannot inspect existing broker positions — "
                f"{type(exc).__name__}: {exc}. Batch skipped.[/bold red]"
            )
            return
        occupied = {
            str(p.get("symbol"))
            for p in existing_positions
            if float(p.get("qty") or 0) != 0
        }
        occupied.update(self._client_positions)

        tradable: list[EntrySignal] = []
        for signal in batch.signals:
            if not self._signal_is_fresh(signal):
                console.print(
                    f"[red][!] STALE — {signal.ticker} past entry_cutoff_at; skipping[/red]"
                )
                continue
            if (
                signal.side.upper() == "SELL"
                and not self._settings.ALLOW_SHORT_SELLING
            ):
                console.print(
                    f"[yellow][!] SHORT — {signal.ticker} SELL signal ignored "
                    "(ALLOW_SHORT_SELLING=false)[/yellow]"
                )
                continue
            if signal.ticker in occupied:
                console.print(
                    f"[yellow][!] OCCUPIED — {signal.ticker} already has a broker "
                    "or client-tracked position; skipping so unrelated holdings "
                    "are never touched.[/yellow]"
                )
                continue
            tradable.append(signal)
        if not tradable:
            console.print("[yellow][!] No tradable signals — batch skipped[/yellow]")
            return

        try:
            equity = await self._broker.get_equity()
        except Exception as exc:
            console.print(
                f"[bold red]✗ Cannot fetch account equity — "
                f"{type(exc).__name__}: {exc}. Batch skipped.[/bold red]"
            )
            return
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

        outcomes_list = list(outcomes)
        self._record_fills(outcomes_list)
        self._open_batches[batch.batch_id] = {s.signal_id: s for s in tradable}
        console.print(
            f"[bold green][{batch.batch_id}] Deployed. Tracking only positions "
            "opened by this client; no resting stops were placed. Awaiting the "
            "9:30:15 AM open-cross exit signal.[/bold green]"
        )

    def _record_fills(self, outcomes: list[EntryOutcome]) -> None:
        """Remember signed quantities this client owns; ignore unrelated lots."""
        for outcome in outcomes:
            fill = outcome.fill
            if not outcome.ok or fill is None:
                continue
            qty = int(fill.get("qty", outcome.order.shares))
            signed = qty if outcome.order.side.upper() == "BUY" else -qty
            symbol = outcome.order.ticker
            remaining = self._client_positions.get(symbol, 0.0) + signed
            if remaining:
                self._client_positions[symbol] = remaining
            else:
                self._client_positions.pop(symbol, None)
        self._exited_since_entry = not self._client_positions

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
        if self._using_terminal_confirm and not sys.stdin.isatty():
            console.print(
                "[bold red][!] semi_auto requires an interactive TTY; refusing "
                "to fire without a human confirmation channel.[/bold red]"
            )
            return False
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
        tracked = self._client_positions.get(ticker, 0.0)
        if tracked == 0:
            console.print(
                f"[yellow][!] {ticker} was not opened by this client — refusing "
                "to touch an unrelated holding.[/yellow]"
            )
            return
        qty = min(
            int(abs(tracked) * close_signal.percentage / 100.0),
            int(abs(tracked)),
        )
        if qty <= 0:
            console.print(
                f"[yellow][!] {ticker}: requested percentage rounds to zero "
                "shares — skipping.[/yellow]"
            )
            return
        try:
            summary = await self._broker.close_position(ticker, qty=qty)
            console.print(f"[green][OK][/green] {ticker} — {summary.get('status')}")
        except Exception as exc:
            console.print(
                f"[red][!] Close failed for {ticker} — {type(exc).__name__}: {exc}[/red]"
            )
            return
        remaining = tracked - (qty if tracked > 0 else -qty)
        if remaining:
            self._client_positions[ticker] = remaining
        else:
            self._client_positions.pop(ticker, None)
        self._exited_since_entry = not self._client_positions

    async def _exit_everything(self, reason: str) -> None:
        if not self._client_positions:
            console.print(
                "[dim]No positions opened by this client to exit; unrelated "
                "holdings are untouched.[/dim]"
            )
            self._exited_since_entry = True
            return

        try:
            broker_positions = await self._broker.get_open_positions()
        except Exception as exc:
            console.print(
                f"[red][!] Unable to list positions — {type(exc).__name__}: {exc}. "
                "Keeping client positions tracked; the failsafe will retry.[/red]"
            )
            return

        by_symbol = {
            str(position.get("symbol")): position for position in broker_positions
        }
        closed: list[dict] = []
        exits: list[dict] = []
        for symbol, tracked in list(self._client_positions.items()):
            position = by_symbol.get(symbol)
            if position is None:
                console.print(
                    f"[yellow][!] {symbol}: no broker position found — removing "
                    "from client tracking.[/yellow]"
                )
                self._client_positions.pop(symbol, None)
                continue
            available = int(abs(float(position.get("qty") or 0)))
            desired = min(int(abs(tracked)), available)
            if desired <= 0:
                continue
            try:
                summary = await self._broker.close_position(symbol, qty=desired)
            except Exception as exc:
                console.print(
                    f"[red][!] Exit failed for {symbol} — "
                    f"{type(exc).__name__}: {exc}[/red]"
                )
                continue
            exits.append(summary)
            signed_qty = desired if float(position.get("qty") or 0) > 0 else -desired
            closed.append({**position, "qty": signed_qty})
            self._client_positions.pop(symbol, None)

        self._open_batches.clear()
        self._exited_since_entry = not self._client_positions
        self._print_pnl_table(closed, exits, reason)
        if self._client_positions:
            console.print(
                "[yellow][!] Some client-opened positions remain tracked — the "
                "failsafe will retry; unrelated holdings are untouched.[/yellow]"
            )
        else:
            console.print(
                "[bold green]Client-opened positions exited; unrelated holdings "
                "untouched.[/bold green]"
            )

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

    # ------------------------------------------------------------------ #
    # LOCAL_EXIT_FAILSAFE watchdog
    # ------------------------------------------------------------------ #
    def _in_failsafe_window(self, now: datetime) -> bool:
        local = now.astimezone(ET)
        if local.weekday() >= 5:  # Alpaca is closed on weekends
            return False
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
        if self._exited_since_entry or not self._client_positions:
            return
        console.print(
            "[bold red][FAILSAFE] The 9:30:15 AM EXIT_SIGNAL never arrived "
            "but client-opened positions are still tracked — liquidating "
            "them locally now.[/bold red]"
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
                "client-opened positions rely fully on the signal server."
                "[/yellow]"
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
