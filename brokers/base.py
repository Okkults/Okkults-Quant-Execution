"""Abstract broker interface.

Every broker driver implements this surface so the executor stays
broker-agnostic. All methods are async; synchronous SDKs are wrapped
in ``asyncio.to_thread`` by their implementations.

The executor never places resting stop orders. Exits are explicit market
orders for quantities the client itself opened.
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
        client_order_id: str | None = None,
    ) -> dict:
        """Place a market or limit order; returns a fill/status dict.

        Keys returned by every implementation: ``order_id``, ``symbol``,
        ``qty``, ``side``, ``status``, ``avg_fill_price``.
        """

    @abstractmethod
    async def close_position(
        self,
        symbol: str,
        percentage: float = 100.0,
        qty: int | None = None,
    ) -> dict:
        """Close ``percentage`` (0-100) or an explicit ``qty`` of one position."""


__all__ = ["BaseBroker"]
