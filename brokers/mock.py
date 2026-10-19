"""Mock broker — trustworthy stand-in for local demos and tests.

``MockBroker`` implements the full :class:`BaseBroker` surface against a
simulated ``$50,000`` paper account. Used by:

* ``python main.py --mock`` — lets anyone run the complete terminal flow
  with zero credentials
* the pytest suite — fast, deterministic broker behaviour

Fills are sequential-safe but intentionally instant-fast so the 12-ticker
concurrent-batch latency ceiling (<200 ms) can be asserted.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

from brokers.base import BaseBroker

MOCK_EXAMPLE_TICKERS: list[str] = [
    "SMPL", "NVAX", "ORIC", "CRSP", "BEAM", "VKTX",
    "RXRX", "SANA", "TILX", "CGEN", "NUMG", "ABSI",
]


class MockBroker(BaseBroker):
    """Zero-credential double that fakes realistic order behaviour."""

    name = "mock"

    def __init__(self, equity: float = 50_000.0, fill_delay: float = 0.02) -> None:
        self._equity = equity
        self._fill_delay = fill_delay
        self._positions: dict[str, dict[str, Any]] = {}
        self.orders: list[dict[str, Any]] = []
        self.closed_symbols: list[tuple[str, float]] = []
        self.cancelled: int = 0
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        return True

    async def get_equity(self) -> float:
        return self._equity

    async def get_open_positions(self) -> list[dict]:
        return [
            {
                "symbol": s,
                "qty": float(p["qty"]),
                "side": "BUY" if p["qty"] > 0 else "SELL",
                "avg_entry_price": float(p["avg_entry_price"]),
            }
            for s, p in self._positions.items()
        ]

    async def place_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str,
        limit_price: float | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        await asyncio.sleep(self._fill_delay)
        order_id = f"mock-{next(self._ids):04d}"
        price = 100.0 + (len(symbol) % 3) * 0.25  # deterministic fake price
        if order_type == "stop" and stop_price is not None:
            price = stop_price
        elif order_type == "limit" and limit_price is not None:
            price = limit_price
        record = {
            "order_id": order_id,
            "type": order_type,
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "status": "FILLED" if order_type != "stop" else "PLACED",
            "avg_fill_price": float(price),
        }
        self.orders.append(record)
        if order_type != "stop":
            self._open_position(record)
        return record

    async def place_bracket_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        stop_price: float,
        take_profit_price: float | None = None,
    ) -> dict:
        fill = await self.place_order(symbol, qty, side, "market")
        await asyncio.sleep(self._fill_delay)
        stop_id = f"mock-{next(self._ids):04d}"
        stop_record = {
            "order_id": stop_id,
            "type": "stop",
            "symbol": symbol,
            "qty": qty,
            "side": "SELL" if side == "BUY" else "BUY",
            "status": "PLACED",
            "avg_fill_price": float(stop_price),
        }
        self.orders.append(stop_record)
        return {**fill, "bracket_stop_id": stop_id}

    async def close_all_positions(self) -> list[dict]:
        results: list[dict] = []
        for symbol in list(self._positions.keys()):
            results.append(await self.close_position(symbol, 100.0))
        return results

    async def close_position(self, symbol: str, percentage: float) -> dict:
        position = self._positions.get(symbol)
        if position is None:
            return {"symbol": symbol, "status": "SKIPPED"}
        closed_qty = int(abs(position["qty"]) * percentage / 100.0)
        exit_record = await self.place_order(
            symbol, closed_qty, "SELL" if position["qty"] > 0 else "BUY", "market"
        )
        if percentage >= 100.0:
            self.closed_symbols.append((symbol, float(position["avg_entry_price"])))
            self._positions.pop(symbol, None)
        return {"symbol": symbol, "status": "CLOSED", "raw": exit_record}

    async def cancel_all_orders(self) -> int:
        resting = [o for o in self.orders if o["status"] == "PLACED"]
        for order in resting:
            order["status"] = "CANCELED"
        self.cancelled += len(resting)
        return len(resting)

    def _open_position(self, fill: dict) -> None:
        symbol = fill["symbol"]
        qty = fill["qty"] if fill["side"] == "BUY" else -fill["qty"]
        entry = float(fill["avg_fill_price"] or 100.0)
        if symbol in self._positions:
            self._positions[symbol]["qty"] += qty
        else:
            self._positions[symbol] = {"qty": qty, "avg_entry_price": entry}
