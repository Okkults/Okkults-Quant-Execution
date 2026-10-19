"""Sizing engine parity tests — mirrors the website dashboard exactly."""

import pytest

from core.risk import SizingResult, build_scaling_warning, calculate_order_sizes


def _signal(i: int, ticker: str, price: float, side: str = "BUY") -> dict:
    return {
        "signal_id": f"sig-{i:02d}",
        "ticker": ticker,
        "side": side,
        "reference_price": price,
        "limit_price": None,
        "entry_cutoff_at": "2026-10-19T15:58:00-04:00",
        "exit_rules": {"disaster_stop_pct": 0.07, "batch_id": "b1"},
    }


def make_signals(n: int, price: float = 208.0) -> list[dict]:
    return [_signal(i, f"T{i:02d}", price) for i in range(n)]


def test_worked_example_from_spec() -> None:
    """$50k equity, 10% target, 30% cap, 6 signals, $208 reference price.

    Spec's expected output: effective 5%, 12 shares, $2,496 per order,
    amber scaling warning raised.
    """
    result = calculate_order_sizes(
        signals=make_signals(6),
        account_equity=50_000.0,
        target_pct=0.10,
        max_total_risk_pct=0.30,
    )
    assert isinstance(result, SizingResult)
    assert result.scaling_applied is True
    assert result.effective_pct == 0.05
    assert len(result.orders) == 6
    for order in result.orders:
        assert order.shares == 12
        assert order.dollar_allocation == 2496.0  # 12 * $208
    assert build_scaling_warning(6, 0.10, result.effective_pct) == (
        "⚠ Risk Budget Auto-Scaled: 6 setups detected. Sizing adjusted "
        "from 10.0% down to 5.0% per ticker to prevent over-allocation."
    )


def test_no_scaling_when_under_double_cap() -> None:
    target = 0.06
    result = calculate_order_sizes(
        signals=make_signals(2, price=100.0),
        account_equity=50_000.0,
        target_pct=target,
        max_total_risk_pct=0.30,
    )
    assert result.scaling_applied is False
    assert result.effective_pct == 0.06
    # 50_000 * 0.06 = $3,000 -> 30 whole shares at $100
    assert all(o.shares == 30 for o in result.orders)


def test_exactly_at_cap_does_not_scale() -> None:
    """3 x 10% == 30% cap exactly -> no downscale."""
    result = calculate_order_sizes(
        signals=make_signals(3, price=100.0),
        account_equity=50_000.0,
        target_pct=0.10,
        max_total_risk_pct=0.30,
    )
    assert result.scaling_applied is False
    assert result.effective_pct == 0.10


def test_per_trade_notional_cap_smaller_than_allocation() -> None:
    """$1,000 cap beats the $2,500 allocation (6 signals @ 5%)."""
    result = calculate_order_sizes(
        signals=make_signals(6, price=50.0),
        account_equity=50_000.0,
        target_pct=0.10,
        max_total_risk_pct=0.30,
        per_trade_notional_cap=1_000.0,
    )
    for order in result.orders:
        assert order.dollar_allocation <= 1_000.0
        assert order.shares == 20  # $1,000 // $50


def test_zero_price_signals_are_skipped() -> None:
    result = calculate_order_sizes(
        signals=[_signal(0, "ZERO", 0.0), _signal(1, "GOOD", 100.0)],
        account_equity=50_000.0,
        target_pct=0.02,
        max_total_risk_pct=0.30,
    )
    assert len(result.orders) == 1
    assert result.orders[0].ticker == "GOOD"
    assert ("ZERO", "zero/negative reference_price") in result.skipped


def test_cash_reserve_shrinks_tradable_equity() -> None:
    result = calculate_order_sizes(
        signals=make_signals(1, price=100.0),
        account_equity=50_000.0,
        target_pct=0.10,
        max_total_risk_pct=0.30,
        cash_reserve=25_000.0,
    )
    # (50k - 25k) * 10% = $2,500 -> 25 shares at $100
    assert result.orders[0].shares == 25


def test_tiny_equity_skips_unaffordable_signals() -> None:
    result = calculate_order_sizes(
        signals=make_signals(1, price=999.0),
        account_equity=100.0,
        target_pct=0.10,
        max_total_risk_pct=0.30,
    )
    assert not result.orders
    assert result.skipped == [("T00", "allocation too small for 1 share")]


def test_no_signals_yields_empty_result() -> None:
    result = calculate_order_sizes(
        signals=[],
        account_equity=50_000.0,
        target_pct=0.10,
        max_total_risk_pct=0.30,
    )
    assert result.orders == []
    assert result.scaling_applied is False


def test_defaults_are_five_and_thirty_percent() -> None:
    result = calculate_order_sizes(
        signals=make_signals(1, price=100.0),
        account_equity=50_000.0,
    )
    assert result.effective_pct == 0.05
    assert result.orders[0].shares == 25


def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        calculate_order_sizes(
            signals=make_signals(1), account_equity=-1, target_pct=0.1,
            max_total_risk_pct=0.3,
        )
    with pytest.raises(ValueError):
        calculate_order_sizes(
            signals=make_signals(1), account_equity=100, target_pct=0,
            max_total_risk_pct=0.3,
        )
    with pytest.raises(ValueError):
        calculate_order_sizes(
            signals=make_signals(1), account_equity=100, target_pct=0.1,
            max_total_risk_pct=0.31,
        )


def test_sizing_engine_enforces_hard_caps() -> None:
    with pytest.raises(ValueError):
        calculate_order_sizes(
            signals=make_signals(1), account_equity=100, target_pct=0.11,
            max_total_risk_pct=0.30,
        )
    with pytest.raises(ValueError):
        calculate_order_sizes(
            signals=make_signals(1), account_equity=100, target_pct=0.10,
            max_total_risk_pct=0.31,
        )


def test_side_passed_through_for_short_entries() -> None:
    sells = [_signal(0, "SHT0", 100.0, side="SELL")]
    result = calculate_order_sizes(
        signals=sells,
        account_equity=50_000.0,
        target_pct=0.10,
        max_total_risk_pct=0.30,
    )
    assert result.orders[0].side == "SELL"
    assert result.orders[0].shares == 50
