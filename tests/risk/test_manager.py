import pytest
from src.risk.manager import compute_position_size, SizeResult

def test_compute_position_size_fast_paths():
    """
    Test edge cases where atr_value <= 0 or current_price <= 0.
    The function should return a 0-filled SizeResult.
    """
    base_args = {
        "capital": 10000.0,
        "risk_per_trade_pct": 1.0,
        "stop_atr_mult": 2.0,
        "max_position_pct": 20.0,
        "max_leverage": 20.0,
    }

    expected = SizeResult(size_usd=0, size_base=0, risk_usd=0, leverage_used=0)

    # atr_value = 0
    assert compute_position_size(**base_args, atr_value=0.0, current_price=100.0) == expected

    # atr_value < 0
    assert compute_position_size(**base_args, atr_value=-5.0, current_price=100.0) == expected

    # current_price = 0
    assert compute_position_size(**base_args, atr_value=2.0, current_price=0.0) == expected

    # current_price < 0
    assert compute_position_size(**base_args, atr_value=2.0, current_price=-10.0) == expected

    # Both <= 0
    assert compute_position_size(**base_args, atr_value=0.0, current_price=-5.0) == expected
