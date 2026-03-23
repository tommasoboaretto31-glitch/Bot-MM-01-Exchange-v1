import pytest
from src.risk.manager import compute_position_size, SizeResult

def test_compute_position_size_basic():
    """Validates standard calculation."""
    # capital=10000, risk=1%, ATR=2, mult=2, price=100
    # risk_usd = 10000 * 0.01 = 100
    # stop_distance = 2 * 2 = 4
    # size_base = 100 / 4 = 25.0
    # size_usd = 25 * 100 = 2500.0
    # leverage_used = 2500 / 10000 = 0.25
    # Note: max_position_pct defaults to 20.0, so we must set it higher to avoid capping

    result = compute_position_size(
        capital=10000.0,
        risk_per_trade_pct=1.0,
        atr_value=2.0,
        stop_atr_mult=2.0,
        current_price=100.0,
        max_position_pct=30.0
    )

    assert result.risk_usd == 100.0
    assert result.size_base == 25.0
    assert result.size_usd == 2500.0
    assert result.leverage_used == 0.25

def test_compute_position_size_zero_or_negative():
    """Validates zeroed result when ATR or price is <= 0."""
    # Zero ATR
    res1 = compute_position_size(1000, 1, 0, 2, 100)
    assert res1.size_usd == 0
    assert res1.size_base == 0

    # Negative ATR
    res2 = compute_position_size(1000, 1, -1, 2, 100)
    assert res2.size_usd == 0

    # Zero price
    res3 = compute_position_size(1000, 1, 2, 2, 0)
    assert res3.size_usd == 0

    # Negative price
    res4 = compute_position_size(1000, 1, 2, 2, -10)
    assert res4.size_usd == 0

def test_compute_position_size_limits():
    """Validates capping by max_position_pct and max_leverage."""
    capital = 10000.0

    # Standard: risk_usd=1000, stop_distance=2, size_base=500, size_usd=50000, leverage=5
    # Capped by max_position_pct=10%: size_usd=1000
    res1 = compute_position_size(
        capital=capital,
        risk_per_trade_pct=10.0,
        atr_value=1.0,
        stop_atr_mult=2.0,
        current_price=100.0,
        max_position_pct=10.0,
        max_leverage=20.0
    )
    assert res1.size_usd == 1000.0
    assert res1.leverage_used == 0.1

    # Capped by max_leverage=1.0: size_usd=10000
    # Standard: risk_usd=1000, stop_distance=2, size_base=500, size_usd=50000, leverage=5
    res2 = compute_position_size(
        capital=capital,
        risk_per_trade_pct=10.0,
        atr_value=1.0,
        stop_atr_mult=2.0,
        current_price=100.0,
        max_position_pct=100.0,
        max_leverage=1.0
    )
    assert res2.size_usd == 10000.0
    assert res2.leverage_used == 1.0

def test_compute_stop_loss():
    """Validates adaptive stop-loss calculation."""
    from src.risk.manager import compute_stop_loss

    entry = 100.0
    atr = 2.0
    # range: mult=1.5, trend: mult=2.5

    # BUY, range
    assert compute_stop_loss(entry, "BUY", atr, regime="range") == 100.0 - (2.0 * 1.5)

    # BUY, trend
    assert compute_stop_loss(entry, "BUY", atr, regime="trend") == 100.0 - (2.0 * 2.5)

    # SELL, range
    assert compute_stop_loss(entry, "SELL", atr, regime="range") == 100.0 + (2.0 * 1.5)

    # SELL, trend
    assert compute_stop_loss(entry, "SELL", atr, regime="trend") == 100.0 + (2.0 * 2.5)

def test_drawdown_monitor_basic():
    """Validates initialization and basic capital updates."""
    from src.risk.manager import DrawdownMonitor

    mon = DrawdownMonitor(max_daily_drawdown_pct=5.0)
    mon.initialize(capital=10000.0, timestamp=1000)

    state = mon.update(current_capital=9800.0, timestamp=1010)
    assert state.current_capital == 9800.0
    assert state.pnl_today == -200.0
    assert state.current_drawdown_pct == 2.0
    assert state.max_drawdown_pct == 2.0
    assert state.is_halted is False

    mon.update(current_capital=10500.0, timestamp=1020)
    assert mon.state.pnl_today == 500.0
    assert mon.state.current_drawdown_pct == 0.0
    assert mon.state.max_drawdown_pct == 2.0  # Max drawdown is remembered

def test_drawdown_monitor_halt():
    """Validates that the monitor correctly halts when threshold is reached."""
    from src.risk.manager import DrawdownMonitor

    mon = DrawdownMonitor(max_daily_drawdown_pct=5.0)
    mon.initialize(capital=10000.0, timestamp=1000)

    # Drawdown of 6% (9400)
    state = mon.update(current_capital=9400.0, timestamp=1010)
    assert state.is_halted is True
    assert "exceeded max 5.0" in state.halt_reason

def test_drawdown_monitor_reset():
    """Validates 24h reset logic."""
    from src.risk.manager import DrawdownMonitor
    from unittest.mock import patch

    mon = DrawdownMonitor(max_daily_drawdown_pct=5.0)

    with patch('time.time', return_value=1000):
        mon.initialize(capital=10000.0)

    # Update within 24h
    mon.update(current_capital=9900.0, timestamp=1000 + 3600)
    assert mon.state.day_start_capital == 10000.0
    assert mon.state.pnl_today == -100.0

    # Update after 24h (86400 seconds)
    # It should reset with 9900 as the new day start capital
    state = mon.update(current_capital=9900.0, timestamp=1000 + 86401)
    assert state.day_start_capital == 9900.0
    assert state.pnl_today == 0.0
