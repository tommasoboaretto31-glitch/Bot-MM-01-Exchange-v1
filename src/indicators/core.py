"""
Technical indicator calculations.

All functions operate on pandas DataFrames with OHLCV columns
and return Series or DataFrames with indicator values.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ? VWAP ?

def vwap(df: pd.DataFrame, session_hours: int = 24) -> pd.Series:
    """
    Volume-Weighted Average Price with session-based reset.

    Args:
        df: OHLCV DataFrame with DatetimeIndex
        session_hours: Reset VWAP after this many hours

    Returns:
        Series with VWAP values
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"]

    # Create session grouper
    session_td_ns = pd.Timedelta(hours=session_hours).value
    
    if isinstance(df.index, pd.DatetimeIndex):
        ts_ns = df.index.values.astype(np.int64)
        first_ns = df.index[0].value
    else:
        # Fallback for integer index (assume milliseconds if large)
        ts_ns = df.index.values.astype(np.int64)
        if len(ts_ns) > 0 and ts_ns[0] > 1e12: # Likely milliseconds or nanoseconds
             # If it's already nanoseconds, good. If ms, convert to ns.
             if ts_ns[0] < 1e15: ts_ns *= 1_000_000
        first_ns = ts_ns[0] if len(ts_ns) > 0 else 0

    session_id = (ts_ns - first_ns) // session_td_ns
    session_id = session_id.astype(int)

    cum_tvol = (typical * vol).groupby(session_id).cumsum()
    cum_vol = vol.groupby(session_id).cumsum()

    result = cum_tvol / cum_vol.replace(0, np.nan)
    return result.rename("vwap")


def vwap_distance(df: pd.DataFrame, vwap_series: pd.Series) -> pd.Series:
    """Percentage distance from VWAP: (close - vwap) / vwap * 100."""
    dist = (df["close"] - vwap_series) / vwap_series * 100
    return dist.rename("vwap_distance")


# ? RSI ?

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index.

    Args:
        series: Price series (typically close)
        period: Lookback period

    Returns:
        RSI values [0, 100]
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.rename("rsi")


# ? ADX ?

def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Average Directional Index with +DI and -DI.

    Returns:
        DataFrame with columns: adx, plus_di, minus_di
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    # True Range
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Directional movement
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
    )

    # Smoothed with EMA
    atr_val = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr_val)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr_val)

    # ADX
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx_val = dx.ewm(span=period, adjust=False).mean()

    return pd.DataFrame(
        {"adx": adx_val, "plus_di": plus_di, "minus_di": minus_di},
        index=df.index,
    )


# ? ATR ?

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    return tr.ewm(span=period, adjust=False).mean().rename("atr")


# ? Momentum ?

def momentum(series: pd.Series, period: int = 10) -> pd.Series:
    """Rate of Change (ROC): (price - price_n_ago) / price_n_ago * 100."""
    roc = (series - series.shift(period)) / series.shift(period) * 100
    return roc.rename("momentum")


# ? Realized Volatility ?

def realized_volatility(series: pd.Series, period: int = 20) -> pd.Series:
    """Rolling standard deviation of log returns."""
    log_ret = np.log(series / series.shift(1))
    return log_ret.rolling(period).std().rename("realized_vol")


# ? CVD (Cumulative Volume Delta) ?

def cvd(df: pd.DataFrame) -> pd.Series:
    """
    Cumulative Volume Delta from buy/sell volume per candle.
    
    Requires 'delta' column (buy_volume - sell_volume) in the DataFrame.
    If not present, falls back to a simple price-based delta estimate.
    """
    if "delta" in df.columns:
        return df["delta"].cumsum().rename("cvd")
    
    # Fallback: estimate delta from price direction * volume
    direction = np.sign(df["close"] - df["open"])
    estimated_delta = direction * df["volume"]
    return estimated_delta.cumsum().rename("cvd")


# ? Divergence Detection ?

def detect_divergence(
    price: pd.Series,
    indicator: pd.Series,
    lookback: int = 14,
    min_swing: float = 0.001,
) -> pd.Series:
    """
    Detect price vs indicator divergence using rolling swing highs/lows.
    
    Returns:
        Series with values: +1 (bullish divergence), -1 (bearish divergence), 0 (none)
        
    Bullish divergence: price makes lower low, indicator makes higher low
    Bearish divergence: price makes higher high, indicator makes lower high
    """
    result = pd.Series(0, index=price.index, dtype=float)
    
    if len(price) < lookback * 2:
        return result.rename("divergence")
    
    # Rolling highs and lows  
    price_roll_high = price.rolling(lookback).max()
    price_roll_low = price.rolling(lookback).min()
    ind_roll_high = indicator.rolling(lookback).max()
    ind_roll_low = indicator.rolling(lookback).min()
    
    # Previous window highs/lows
    price_prev_high = price.shift(lookback).rolling(lookback).max()
    price_prev_low = price.shift(lookback).rolling(lookback).min()
    ind_prev_high = indicator.shift(lookback).rolling(lookback).max()
    ind_prev_low = indicator.shift(lookback).rolling(lookback).min()
    
    # Bearish divergence: price higher high BUT indicator lower high
    bearish = (
        (price_roll_high > price_prev_high * (1 + min_swing)) &
        (ind_roll_high < ind_prev_high * (1 - min_swing))
    )
    
    # Bullish divergence: price lower low BUT indicator higher low
    bullish = (
        (price_roll_low < price_prev_low * (1 - min_swing)) &
        (ind_roll_low > ind_prev_low * (1 + min_swing))
    )
    
    result = result.where(~bearish, -1)
    result = result.where(~bullish, 1)
    
    return result.rename("divergence")


# ? OI Change Detection ?

def oi_change_signal(oi_series: pd.Series, price: pd.Series, period: int = 5) -> pd.Series:
    """
    Open Interest change signal.
    
    Returns: Series with signal interpretation:
      +2: OI rising + price rising = strong bull
      +1: OI falling + price rising = short squeeze (weak bull)  
      -2: OI rising + price falling = strong bear
      -1: OI falling + price falling = long liquidation (weak bear)
       0: neutral / insufficient data
    """
    if oi_series is None or oi_series.empty:
        return pd.Series(0, index=price.index).rename("oi_signal")
    
    oi_change = oi_series.pct_change(period)
    price_change = price.pct_change(period)
    
    result = pd.Series(0, index=price.index, dtype=float)
    
    # Strong signals (OI rising)
    result = result.where(
        ~((oi_change > 0.01) & (price_change > 0)), 2   # OI↑ Price↑ = strong bull
    )
    result = result.where(
        ~((oi_change > 0.01) & (price_change < 0)), -2  # OI↑ Price↓ = strong bear
    )
    # Weak signals (OI falling)
    result = result.where(
        ~((oi_change < -0.01) & (price_change > 0)), 1  # OI↓ Price↑ = squeeze
    )
    result = result.where(
        ~((oi_change < -0.01) & (price_change < 0)), -1 # OI↓ Price↓ = liquidation
    )
    
    return result.rename("oi_signal")


# ? Composite Calculator ?

def compute_all(
    df: pd.DataFrame,
    rsi_period: int = 14,
    adx_period: int = 14,
    atr_period: int = 14,
    momentum_period: int = 10,
    vwap_session_hours: int = 24,
    oi_series: pd.Series | None = None,
) -> pd.DataFrame:
    """
    Compute all indicators and merge into single DataFrame.

    Returns:
        Original OHLCV + all indicator columns including CVD and divergences
    """
    result = df.copy()

    # VWAP
    v = vwap(df, session_hours=vwap_session_hours)
    result["vwap"] = v
    result["vwap_distance"] = vwap_distance(df, v)

    # RSI
    result["rsi"] = rsi(df["close"], period=rsi_period)

    # ADX
    adx_df = adx(df, period=adx_period)
    result = pd.concat([result, adx_df], axis=1)

    # ATR
    result["atr"] = atr(df, period=atr_period)

    # Momentum
    result["momentum"] = momentum(df["close"], period=momentum_period)

    # Realized volatility
    result["realized_vol"] = realized_volatility(df["close"])

    # CVD (requires buy_volume/sell_volume or delta column)
    result["cvd"] = cvd(df)

    # Divergence detection: Price vs RSI
    rsi_series = result["rsi"].copy()
    result["rsi_divergence"] = detect_divergence(
        df["close"], rsi_series, lookback=rsi_period, min_swing=0.001
    )

    # Divergence detection: Price vs CVD
    cvd_series = result["cvd"].copy()
    result["cvd_divergence"] = detect_divergence(
        df["close"], cvd_series, lookback=rsi_period, min_swing=0.005
    )

    # OI signal (if OI data is provided)
    if oi_series is not None and not oi_series.empty:
        result["oi_signal"] = oi_change_signal(oi_series, df["close"])
    else:
        result["oi_signal"] = 0

    return result
