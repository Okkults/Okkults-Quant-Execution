"""Alpaca broker driver.

Implements :class:`brokers.base.BaseBroker` with ``alpaca-py``.
All synchronous Alpaca calls are wrapped in ``asyncio.to_thread`` so the
executor's event loop never blocks on I/O.

Security: API keys are used only inside this process. They are never
logged, never transmitted to the Okkults signal server, and never leave
the user's machine.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from alpaca.common.exceptions import APIError as AlpacaAPIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from rich.console import Console

from brokers.base import BaseBroker

console = Console()


class AlpacaBrokerError(Exception):
    """Raised when Alpaca reports an error we can surface cleanly."""


def _mask(key: str) -> str:
    """Show only the last 3 characters of a credential-shaped string."""
    if not key:
        return "<not set>"
    return f"***{key[-3:]}"


def _attr(obj: object, name: str) -> Any:
    """alpaca-py returns Union[Model, dict]; extract whether shaped either way."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _numeric(value: Any) -> float | None:
    """Best-effort float coercion over Union str/int/float/None fields."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class AlpacaBroker(BaseBroker):
    """Async wrapper around the synchronous alpaca-py TradingClient."""

    name = "alpaca"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
    ) -> None:
        if not api_key or not secret_key:
            raise AlpacaBrokerError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are empty. "
                "Run `python main.py --onboard` to configure the terminal."
            )
        self._client = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
            raw_data=True,
        )
        self.paper = paper
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ #
    # connectivity
    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        try:
            account = await asyncio.to_thread(self._client.get_account)
        except AlpacaAPIError as exc:  # broker rejected the credentials
            console.print(f"[red][!] Alpaca account fetch failed: {exc}[/red]")
            return False
        except Exception as exc:  # network / DNS / TLS failures
            console.print(f"[red][!] Alpaca unreachable: {exc}[/red]")
            return False
        status = str(_attr(account, "status") or "UNKNOWN")
        self._connected = status == "ACTIVE"
        mode = "[green]PAPER[/green]" if self.paper else "[red]LIVE[/red]"
        console.print(
            f"[cyan][OK][/cyan] Alpaca connected -> {mode} account (status={status})"
        )
        return self._connected

    # ------------------------------------------------------------------ #
    # account state
    # ------------------------------------------------------------------ #
    async def get_equity(self) -> float:
        """Return current account equity (requested live at every sizing)."""
        account = await asyncio.to_thread(self._client.get_account)
        return _numeric(_attr(account, "equity")) or 0.0

    async def get_account_summary(self) -> dict[str, Any]:
        """Equity, cash, status + paper/live flag (used by pre-flight)."""
        account = await asyncio.to_thread(self._client.get_account)
        return {
            "equity": _numeric(_attr(account, "equity")) or 0.0,
            "cash": _numeric(_attr(account, "cash")) or 0.0,
            "status": str(_attr(account, "status") or "UNKNOWN"),
            "paper": self.paper,
        }

    async def get_open_positions(self) -> list[dict]:
        positions = await asyncio.to_thread(self._client.get_all_positions)
        results: list[dict] = []
        for p in list(positions):
            qty = _numeric(_attr(p, "qty")) or 0.0
            results.append(
                {
                    "symbol": str(_attr(p, "symbol") or "?"),
                    "qty": qty,
                    "side": "BUY" if qty > 0 else "SELL",
                    "avg_entry_price": _numeric(_attr(p, "avg_entry_price")) or 0.0,
                    "current_price": _numeric(_attr(p, "current_price")),
                    "unrealized_pl": _numeric(_attr(p, "unrealized_pl")),
                }
            )
        return results

    # ------------------------------------------------------------------ #
    # order placement
    # ------------------------------------------------------------------ #
    async def place_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str,
        limit_price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """Place a market or limit order. Returns a normalized dict."""
        order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
        coid = client_order_id or f"okq-{uuid.uuid4().hex[:12]}"
        notional_type: str
        req: LimitOrderRequest | MarketOrderRequest
        try:
            if order_type == "limit" and limit_price is not None:
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(limit_price, 2),
                    client_order_id=coid,
                )
                notional_type = "limit"
            else:
                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    type=OrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=coid,
                )
                notional_type = "market"

            raw = await asyncio.to_thread(self._client.submit_order, req)
            result = self._normalize_order(raw, symbol, qty, side)
            console.print(
                f"[cyan][OK][/cyan] {notional_type.upper()} submitted "
                f"{side} {qty} {symbol} -> order={result.get('order_id', '?')}"
            )
            return result
        except AlpacaAPIError as exc:
            console.print(f"[red][!] Alpaca rejected {side} {symbol}: {exc}[/red]")
            raise AlpacaBrokerError(f"order rejected for {symbol}: {exc}") from exc

    async def close_position(
        self,
        symbol: str,
        percentage: float = 100.0,
        qty: int | None = None,
    ) -> dict:
        """Close ``percentage``% or an explicit whole-share ``qty`` at market.

        The executor passes the exact tracked quantity and refuses to submit
        entries for symbols that already hold broker positions. Alpaca nets
        lots per symbol, so a position opened externally *after* entry cannot
        be distinguished from the tracked one.
        """
        positions = await self.get_open_positions()
        match = next((p for p in positions if p["symbol"] == symbol), None)
        if match is None:
            raise AlpacaBrokerError(f"no open position for {symbol}")
        available = int(abs(float(match["qty"])))
        close_qty = qty if qty is not None else int(available * percentage / 100.0)
        close_qty = min(max(close_qty, 0), available)
        if close_qty == 0:
            return {"symbol": symbol, "status": "SKIPPED", "qty": 0}
        exit_side = "SELL" if float(match["qty"]) > 0 else "BUY"
        fill = await self.place_order(symbol, close_qty, exit_side, "market")
        return {"symbol": symbol, "status": "CLOSED", "raw": fill}

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_order(raw: Any, symbol: str, qty: int, side: str) -> dict:
        fill_price = _numeric(_attr(raw, "filled_avg_price"))
        return {
            "order_id": str(_attr(raw, "id") or ""),
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "status": str(_attr(raw, "status") or "unknown"),
            "avg_fill_price": fill_price,
        }
