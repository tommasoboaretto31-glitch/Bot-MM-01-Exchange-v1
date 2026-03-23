import numpy as np
import pandas as pd
from src.indicators.core import vwap

def test_vwap_datetime_index():
    # Setup standard DataFrame with DatetimeIndex
    dates = pd.date_range("2023-01-01", periods=10, freq="1h")
    df = pd.DataFrame({
        "high": np.linspace(10, 20, 10),
        "low": np.linspace(8, 18, 10),
        "close": np.linspace(9, 19, 10),
        "volume": np.ones(10) * 100
    }, index=dates)

    result = vwap(df, session_hours=24)

    assert isinstance(result, pd.Series)
    assert result.name == "vwap"
    assert len(result) == 10
    assert not result.isna().all()

def test_vwap_integer_index_ms():
    # Integer index in milliseconds (1e12 to 1e15 range)
    ms_index = pd.Index([1600000000000 + i * 3600000 for i in range(10)])
    df = pd.DataFrame({
        "high": np.linspace(10, 20, 10),
        "low": np.linspace(8, 18, 10),
        "close": np.linspace(9, 19, 10),
        "volume": np.ones(10) * 100
    }, index=ms_index)

    result = vwap(df, session_hours=24)

    assert isinstance(result, pd.Series)
    assert result.name == "vwap"
    assert len(result) == 10
    assert not result.isna().all()

def test_vwap_integer_index_ns():
    # Integer index in nanoseconds (> 1e15)
    ns_index = pd.Index([1600000000000000000 + i * 3600000000000 for i in range(10)])
    df = pd.DataFrame({
        "high": np.linspace(10, 20, 10),
        "low": np.linspace(8, 18, 10),
        "close": np.linspace(9, 19, 10),
        "volume": np.ones(10) * 100
    }, index=ns_index)

    result = vwap(df, session_hours=24)

    assert isinstance(result, pd.Series)
    assert result.name == "vwap"
    assert len(result) == 10
    assert not result.isna().all()

def test_vwap_empty_index():
    # Empty DataFrame fallback
    df = pd.DataFrame(columns=["high", "low", "close", "volume"], dtype=float)
    result = vwap(df, session_hours=24)

    assert isinstance(result, pd.Series)
    assert result.name == "vwap"
    assert len(result) == 0
