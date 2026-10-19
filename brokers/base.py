"""Abstract broker interface.

Every broker driver implements this surface so the executor stays
broker-agnostic. All methods are async; synchronous SDKs are wrapped
in ``asyncio.to_thread`` by their implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseBroker(ABC):
    """Non-custodial execution surface against a user-owned account."""

    name: str = "base"

    @abstractmethod
    async def connect(self) -> bool:
        """Authenticate and verify reachability. True on success."""

    @abstractmethod
    async def get_equity(self) -> float:
        """Current total account equity in USD."""

    @abstractmethod
    async def get_open_positions(self) -> list[dict]:
        """Open positions as dicts: symbol, qty, side, avg_entry_price."""

    @abstractmethod
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
        """Place a market, limit, or stop order; returns a fill/status dict.

        Keys returned by every implementation: ``order_id``, ``symbol``,
        ``qty``, ``side``, ``status``, ``avg_fill_price``.
        """

    @abstractmethod
    async def place_bracket_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        stop_price: float,
        take_profit_price: float | None = None,
    ) -> dict:
        """Place a bracket (entry + stop-loss [ + take-profit]) order."""

    @abstractmethod
    async def close_all_positions(self) -> list[dict]:
        """Liquidate every open position at market; returns per-position results."""

    @abstractmethod
    async def close_position(self, symbol: str, percentage: float) -> dict:
        """Close ``percentage`` (0-100) of one position at market."""

    @abstractmethod
    async def cancel_all_orders(self) -> int:
        """Cancel every resting order (incl. disaster stops). Returns count."""
