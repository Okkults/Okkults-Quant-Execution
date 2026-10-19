"""Live terminal dashboard: account, positions, signal & order feed.

Observer-only by construction — wraps the broker/listener in thin
recording proxies so the execution path stays byte-for-byte identical.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Any, Awaitable

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from brokers.base import BaseBroker
from signals.listener import ListenerCallbacks
from signals.protocol import BatchSignal, ET, ClosePositionSignal, ExitSignal

REFRESH_SECONDS = 2.0


class EventFeed:
    """Timestamped, bounded memory of signals/orders for the live view."""

    def __init__(self, maxlen: int = 200) -> None:
        self._events: deque[dict[str, str]] = deque(maxlen=maxlen)

    def record(self, kind: str, detail: str) -> None:
        stamp = datetime.now(tz=ET).strftime("%H:%M:%S")
        self._events.append({"time": stamp, "kind": kind, "detail": detail})

    def snapshot(self, last: int = 10) -> list[dict[str, str]]:
        return list(self._events)[-last:]


class ObservingBroker:
    """Pass-through broker that records order ops into an EventFeed."""

    def __init__(self, broker: BaseBroker, feed: EventFeed) -> None:
        self._broker = broker
        self._feed = feed

    def __getattr__(self, name: str) -> Any:  # forward everything else
        return getattr(self._broker, name)

    async def place_order(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._broker.place_order(*args, **kwargs)
        if isinstance(result, dict):
            bits = []
            for key in ("symbol", "qty", "side", "limit_price", "status"):
                if key in result:
                    bits.append(f"{key}={result[key]}")
            self._feed.record("ORDER", " ".join(bits) or str(result))
        else:
            self._feed.record("ORDER", str(result)[:120])
        return result

    async def close_position(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._broker.close_position(*args, **kwargs)
        if isinstance(args, tuple) and args:
            self._feed.record("CLOSE", f"close_position {args[0]}")
        elif "symbol" in kwargs or "ticker" in kwargs:
            self._feed.record(
                "CLOSE", f"close_position {kwargs.get('symbol') or kwargs.get('ticker')}"
            )
        else:
            self._feed.record("CLOSE", f"close_position {result!r}"[:120])
        return result


def observing_callbacks(callbacks: ListenerCallbacks, feed: EventFeed) -> ListenerCallbacks:
    """Wrap dispatch handlers so received signals appear in the feed too."""
    on_batch, on_exit, on_close = callbacks.on_batch, callbacks.on_exit, callbacks.on_close_position

    async def wrapped_batch(batch: BatchSignal) -> None:
        legs = ", ".join(
            f"{s.side} {s.ticker} ref={getattr(s, 'reference_price', '?')}"
            for s in batch.signals
        )
        feed.record("SIGNAL", f"BATCH {getattr(batch, 'batch_id', '?')} [{legs}]")
        await on_batch(batch)

    async def wrapped_exit(exit_signal: ExitSignal) -> None:
        feed.record(
            "SIGNAL",
            f"EXIT action={getattr(exit_signal, 'action', '?')} "
            f"reason={getattr(exit_signal, 'reason', '?')}",
        )
        await on_exit(exit_signal)

    async def wrapped_close(close: ClosePositionSignal) -> None:
        feed.record(
            "SIGNAL", f"CLOSE {getattr(close, 'ticker', '?')} {getattr(close, 'percentage', '?')}"
        )
        await on_close(close)

    return ListenerCallbacks(wrapped_batch, wrapped_exit, wrapped_close)


def _account_panel(account: dict[str, Any] | None, elapsed: str) -> Panel:
    if account is None:
        body: RenderableType = Text("account unavailable…", style="dim")
    else:
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_row("equity", f"${(account.get('equity') or 0):,.2f}")
        cash = account.get("cash")
        table.add_row("cash", "—" if cash is None else f"${cash:,.2f}")
        table.add_row("status", f"{account.get('status', '?')} · "
                                f"{'paper' if account.get('paper') else 'LIVE'}")
        body = table
    return Panel(body, title="ACCOUNT", title_align="left", border_style="cyan")


def _positions_panel(positions: list[dict]) -> Panel:
    if not positions:
        return Panel(Text("no open positions", style="dim"), title="POSITIONS",
                     title_align="left", border_style="cyan")
    table = Table(title=None, header_style="bold")
    for col in ("symbol", "qty", "side", "entry", "last", "unrealized P/L"):
        table.add_column(col)
    for pos in positions:
        pl = pos.get("unrealized_pl")
        pl_color = ""
        if pl is not None:
            pl_color = "green" if pl >= 0 else "red"
        table.add_row(
            str(pos.get("symbol", "?")),
            f"{pos.get('qty', 0):g}",
            str(pos.get("side", "?")),
            f"${pos.get('avg_entry_price', 0) or 0:.2f}",
            f"${pos.get('current_price') or 0:.2f}",
            Text(f"${(pl or 0):+.2f}", style=pl_color),
        )
    return Panel(table, title="POSITIONS", title_align="left", border_style="cyan")


def _feed_panel(feed: EventFeed) -> Panel:
    rows = feed.snapshot()
    lines = [Text(f"{e['time']}  [{e['kind']}]  {e['detail']}", style=_kind_style(e["kind"])) for e in rows]
    if not lines:
        lines = [Text("waiting for signals…", style="dim")]
    return Panel(Group(*lines[-10:]), title="SIGNAL & ORDER FEED",
                 title_align="left", border_style="cyan")


def _kind_style(kind: str) -> str:
    return {
        "SIGNAL": "bold magenta",
        "ORDER": "bold green",
        "CLOSE": "bold yellow",
        "INFO": "dim",
    }.get(kind, "")


def render_dashboard(
    account: dict[str, Any] | None,
    positions: list[dict],
    feed: EventFeed,
    ws_connected: bool,
    armed_for: str,
) -> RenderableType:
    header = Text.assemble(
        ("OKKULTS  ·  ", "bold"),
        ("LIVE DASHBOARD", "bold cyan"),
        ("   ws:", "dim"),
        (f"{'CONNECTED' if ws_connected else 'reconnecting…'}  ", "green" if ws_connected else "red"),
        ("armed for " + armed_for, "dim"),
    )
    return Group(
        header,
        _account_panel(account, armed_for),
        _positions_panel(positions),
        _feed_panel(feed),
        Text("press Ctrl+C to stop (open positions are loudly confirmed, never abandoned)", style="dim"),
    )


async def snapshot_broker(broker: BaseBroker) -> tuple[dict[str, Any] | None, list[dict]]:
    account: dict[str, Any] | None
    try:
        account = await broker.get_account_summary()
    except (AttributeError, Exception):  # noqa: BLE001 — dashboard must never crash the terminal
        try:
            account = {"equity": await broker.get_equity(), "cash": None,
                       "status": "CONNECTED", "paper": True}
        except Exception:  # noqa: BLE001
            account = None
    try:
        positions = await broker.get_open_positions()
    except Exception:
        positions = []
    return account, positions


async def run_dashboard(
    console: Console,
    broker: BaseBroker,
    feed: EventFeed,
    ws_probe: Any = None,
) -> None:
    started = datetime.now(tz=ET)
    with Live(console=console, auto_refresh=False) as live:
        while True:
            account, positions = await snapshot_broker(broker)
            connected = False
            if ws_probe is not None:
                try:
                    connected = bool(ws_probe())
                except Exception:
                    connected = False
            armed = str(datetime.now(tz=ET) - started).split(".")[0]
            live.update(
                render_dashboard(account, positions, feed, connected, armed),
                refresh=True,
            )
            await asyncio.sleep(REFRESH_SECONDS)
