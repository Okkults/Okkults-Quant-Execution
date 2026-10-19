"""Interactive Brokers driver — stubbed.

IB Gateway integration is planned for Phase 2. Every execution method
raises; only :meth:`connect` runs, and it prints the notice.
"""

from __future__ import annotations

from rich.console import Console

from brokers.base import BaseBroker

console = Console()

_NOT_IMPLEMENTED = "IBKR driver coming soon. Switch BROKER=alpaca in your .env."


class IBKRClient(BaseBroker):
    """Placeholder implementation with friendly [COMING SOON] notices."""

    name = "ibkr"

    async def connect(self) -> bool:
        console.print(
            "[yellow][!] Interactive Brokers (IB Gateway) integration is "
            "currently in private beta. Coming in Phase 2. Please use Alpaca."
            "[/yellow]"
        )
        return False

    async def get_equity(self) -> float:  # pragma: no cover - stub
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_open_positions(self) -> list[dict]:  # pragma: no cover
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def place_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str,
        limit_price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:  # pragma: no cover - stub
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def close_position(
        self,
        symbol: str,
        percentage: float = 100.0,
        qty: int | None = None,
    ) -> dict:  # pragma: no cover
        raise NotImplementedError(_NOT_IMPLEMENTED)


__all__ = ["IBKRClient"]
