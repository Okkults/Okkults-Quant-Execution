"""The Sizing Engine.

Deterministic portfolio sizing that mirrors the Okkults web dashboard
exactly (reference: website ``src/lib/sizing.ts``, migration 009):

* tradable equity  = account equity minus a cash reserve (never negative)
* total desired    = number of signals x target allocation pct
* over-budget      -> dynamic downscale so the batch never exceeds the
  portfolio risk ceiling (risk parity across 1-12 signals)
* per-order        -> notional cap, whole shares only, zero-share orders
  are skipped loudly rather than executed broken

This module contains capital-allocation arithmetic ONLY. It holds no
market logic, no indicators, and no alpha: every price and every exit
rule arrives pre-computed from the Okkults signal server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True)
class OrderBlueprint:
    """A fully sized, ready-to-submit order instruction."""

    signal_id: str
    ticker: str
    side: str
    reference_price: float
    shares: int
    dollar_allocation: float
    effective_pct: float


@dataclass(frozen=True)
class SizingResult:
    """Outcome of sizing a whole signal batch."""

    orders: list[OrderBlueprint] = field(default_factory=list)
    scaling_applied: bool = False
    effective_pct: float = 0.0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (ticker, reason)

    @property
    def total_notional(self) -> float:
        return sum(o.shares * o.reference_price for o in self.orders)


TARGET_LABEL: Final[str] = "target allocation"


def build_scaling_warning(
    n_signals: int, target_pct: float, effective_pct: float
) -> str:
    """Amber warning printed when the risk budget forces downscaling.

    >>> build_scaling_warning(6, 0.10, 0.05)
    '⚠ Risk Budget Auto-Scaled: 6 setups detected. Sizing adjusted from 10.0% down to 5.0% per ticker to prevent over-allocation.'
    """
    return (
        f"⚠ Risk Budget Auto-Scaled: {n_signals} setups detected. Sizing adjusted "
        f"from {target_pct * 100:.1f}% down to {effective_pct * 100:.1f}% per ticker "
        f"to prevent over-allocation."
    )


RISK_EPSILON: float = 1e-9  # total_desired_risk float-drift guard


def _effective_pct_for(n_signals: int, target_pct: float, max_total_risk_pct: float) -> float:
    """Share of the portfolio ceiling per signal when the batch over-spends."""
    return round(max_total_risk_pct / n_signals, 6)


def calculate_order_sizes(
    signals: list[dict],
    account_equity: float,
    target_pct: float,
    max_total_risk_pct: float,
    per_trade_notional_cap: float | None = None,
    cash_reserve: float = 0.0,
) -> SizingResult:
    """Turn wire signals into sized order blueprints.

    Parameters mirror the website dashboard:

    ``signals``             server payloads (need ticker, reference_price)
    ``account_equity``      live equity fetched from the broker
    ``target_pct``          MAX_TRADE_ALLOCATION_PCT (e.g. 0.10)
    ``max_total_risk_pct``  MAX_PORTFOLIO_RISK_PCT hard ceiling (e.g. 0.30)
    ``per_trade_notional_cap``  optional USD hard cap per single order
    ``cash_reserve``        dollars held back from sizing
    """
    if account_equity < 0:
        raise ValueError("account_equity must be >= 0")
    if target_pct <= 0 or target_pct > 1:
        raise ValueError("target_pct must be in (0, 1]")
    if max_total_risk_pct <= 0 or max_total_risk_pct > 1:
        raise ValueError("max_total_risk_pct must be in (0, 1]")

    tradable_equity = max(account_equity - cash_reserve, 0.0)
    n_signals = len(signals)

    if n_signals == 0:
        return SizingResult(orders=[], scaling_applied=False, effective_pct=target_pct)

    total_desired_risk = n_signals * target_pct
    if total_desired_risk > max_total_risk_pct + RISK_EPSILON:
        # Risk-parity downscale: the batch as a whole must not exceed the
        # portfolio ceiling, so each signal takes an equal share of it.
        effective_pct = _effective_pct_for(n_signals, target_pct, max_total_risk_pct)
        scaling_applied = True
    else:
        effective_pct = target_pct
        scaling_applied = False

    orders: list[OrderBlueprint] = []
    skipped: list[tuple[str, str]] = []

    for signal in signals:
        ticker = str(signal.get("ticker", "?"))
        reference_price = signal.get("reference_price")
        try:
            price = float(reference_price)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            skipped.append((ticker, "missing/invalid reference_price"))
            continue

        if price <= 0:
            skipped.append((ticker, "zero/negative reference_price"))
            continue

        dollar_allocation = tradable_equity * effective_pct
        if per_trade_notional_cap is not None and dollar_allocation > per_trade_notional_cap:
            dollar_allocation = per_trade_notional_cap

        shares = int(dollar_allocation // price)
        if shares <= 0:
            skipped.append((ticker, "allocation too small for 1 share"))
            continue

        orders.append(
            OrderBlueprint(
                signal_id=str(signal.get("signal_id", "")),
                ticker=ticker,
                side=str(signal.get("side", "BUY")),
                reference_price=price,
                shares=shares,
                dollar_allocation=round(shares * price, 2),
                effective_pct=effective_pct,
            )
        )

    return SizingResult(
        orders=orders,
        scaling_applied=scaling_applied,
        effective_pct=effective_pct,
        skipped=skipped,
    )


__all__ = [
    "TARGET_LABEL",
    "OrderBlueprint",
    "SizingResult",
    "build_scaling_warning",
    "calculate_order_sizes",
]
