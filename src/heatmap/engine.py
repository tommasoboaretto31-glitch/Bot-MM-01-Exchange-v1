"""
Liquidity heatmap engine ? aggregates L2 orderbook data and
computes directional bias for asymmetric grid weighting.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class OrderbookSnapshot:
    """A single L2 orderbook snapshot."""
    timestamp: float
    bids: list[tuple[float, float]]  # [(price, size), ...]
    asks: list[tuple[float, float]]
    mid_price: float = 0.0

    def __post_init__(self):
        if self.bids and self.asks:
            self.mid_price = (self.bids[0][0] + self.asks[0][0]) / 2


@dataclass
class LiquidityBias:
    """Directional bias result from heatmap analysis."""
    score: float          # [-1, +1]: positive = bias long (liquidity above)
    raw_score: float      # Pre-EMA smoothed score
    liquidity_above: float
    liquidity_below: float
    total_liquidity: float
    spread_bps: float     # Bid-ask spread in basis points
    is_anomalous: bool    # True if spread/liquidity is anomalous
    timestamp: float = field(default_factory=time.time)

    @property
    def direction(self) -> str:
        if self.score > 0.1:
            return "LONG"
        elif self.score < -0.1:
            return "SHORT"
        return "NEUTRAL"


class LiquidityHeatmap:
    """
    Aggregates L2 orderbook snapshots over time to build a
    liquidity heatmap and compute directional bias.
    
    The core idea: where liquidity concentrates is where
    price tends to gravitate. If more resting liquidity sits
    above current price, it acts as a magnet (bias long).
    """

    def __init__(
        self,
        rolling_window_minutes: int = 30,
        ema_smoothing: int = 5,
        min_liquidity_ratio: float = 0.1,
        max_spread_bps: float = 100,
        min_spread_bps: float = 5,
    ):
        self.rolling_window = rolling_window_minutes * 60  # seconds
        self.ema_alpha = 2 / (ema_smoothing + 1)
        self.min_liquidity_ratio = min_liquidity_ratio
        self.max_spread_bps = max_spread_bps
        self.min_spread_bps = min_spread_bps

        # Rolling buffer of snapshots
        self._snapshots: deque[OrderbookSnapshot] = deque()
        self._smoothed_bias: float = 0.0
        self._last_bias: LiquidityBias | None = None

    def add_snapshot(self, snapshot: OrderbookSnapshot) -> None:
        """Add a new L2 orderbook snapshot."""
        self._snapshots.append(snapshot)
        self._prune_old()

    def add_from_dict(self, data: dict, timestamp: float | None = None) -> None:
        """
        Add snapshot from raw orderbook dict.
        Expected format: {"bids": [[price, size], ...], "asks": [[price, size], ...]}
        """
        ts = timestamp or time.time()
        bids = [(float(b[0]), float(b[1])) for b in data.get("bids", [])]
        asks = [(float(a[0]), float(a[1])) for a in data.get("asks", [])]
        self.add_snapshot(OrderbookSnapshot(timestamp=ts, bids=bids, asks=asks))

    def _prune_old(self) -> None:
        """Remove snapshots outside the rolling window."""
        cutoff = time.time() - self.rolling_window
        while self._snapshots and self._snapshots[0].timestamp < cutoff:
            self._snapshots.popleft()

    def compute_bias(self, current_price: float | None = None) -> LiquidityBias:
        """
        Compute directional bias from accumulated snapshots.

        Returns:
            LiquidityBias with score in [-1, +1]
        """
        if not self._snapshots:
            return LiquidityBias(
                score=0.0, raw_score=0.0,
                liquidity_above=0.0, liquidity_below=0.0,
                total_liquidity=0.0, spread_bps=0.0,
                is_anomalous=True,
            )

        # Use latest snapshot's mid price as reference
        latest = self._snapshots[-1]
        ref_price = current_price or latest.mid_price

        if ref_price <= 0:
            return LiquidityBias(
                score=0.0, raw_score=0.0,
                liquidity_above=0.0, liquidity_below=0.0,
                total_liquidity=0.0, spread_bps=0.0,
                is_anomalous=True,
            )

        # Aggregate liquidity above and below across all snapshots
        liq_above = 0.0
        liq_below = 0.0

        for snap in self._snapshots:
            # Ask side (above mid) ? size * distance weight
            for price, size in snap.asks:
                if price > ref_price:
                    liq_above += size * price  # USD value
                else:
                    liq_below += size * price

            # Bid side (below mid)
            for price, size in snap.bids:
                if price < ref_price:
                    liq_below += size * price
                else:
                    liq_above += size * price

        total = liq_above + liq_below

        # Compute spread
        spread_bps = 0.0
        if latest.bids and latest.asks:
            best_bid = latest.bids[0][0]
            best_ask = latest.asks[0][0]
            mid = (best_bid + best_ask) / 2
            if mid > 0:
                spread_bps = (best_ask - best_bid) / mid * 10000

        # Anomaly detection
        is_anomalous = (
            spread_bps > self.max_spread_bps
            or total < 1.0  # Virtually no liquidity
            or len(self._snapshots) < 2
        )

        # Raw bias score
        if total > 0:
            raw_score = (liq_above - liq_below) / total
        else:
            raw_score = 0.0

        # Apply minimum ratio filter
        if total > 0:
            ratio = abs(liq_above - liq_below) / total
            if ratio < self.min_liquidity_ratio:
                raw_score = 0.0  # Too balanced ? no meaningful bias

        # EMA smoothing
        self._smoothed_bias = (
            self.ema_alpha * raw_score
            + (1 - self.ema_alpha) * self._smoothed_bias
        )

        # Clamp to [-1, 1]
        score = max(-1.0, min(1.0, self._smoothed_bias))

        bias = LiquidityBias(
            score=score,
            raw_score=raw_score,
            liquidity_above=liq_above,
            liquidity_below=liq_below,
            total_liquidity=total,
            spread_bps=spread_bps,
            is_anomalous=is_anomalous,
        )
        self._last_bias = bias
        return bias

    @property
    def last_bias(self) -> LiquidityBias | None:
        return self._last_bias

    @property
    def snapshot_count(self) -> int:
        return len(self._snapshots)

    def reset(self) -> None:
        """Clear all accumulated data."""
        self._snapshots.clear()
        self._smoothed_bias = 0.0
        self._last_bias = None


class BacktestHeatmap:
    """
    Simulated heatmap for backtesting ? computes bias from
    OHLCV data by estimating liquidity distribution from
    volume profile and price action.
    """

    def __init__(self, ema_smoothing: int = 5, min_ratio: float = 0.1):
        self.ema_alpha = 2 / (ema_smoothing + 1)
        self.min_ratio = min_ratio
        self._smoothed_bias = 0.0

    def compute_from_candles(
        self,
        df_window: "pd.DataFrame",  # noqa: F821
        current_price: float,
    ) -> LiquidityBias:
        """
        Estimate liquidity bias from recent candle data.
        Uses volume profile and price action as proxy for L2 book.

        Logic:
        - High volume at prices above current ? liquidity above (bias long)  
        - High volume at prices below current ? liquidity below (bias short)
        """
        import pandas as pd  # noqa: F811

        if df_window.empty or len(df_window) < 5:
            return LiquidityBias(
                score=0.0, raw_score=0.0,
                liquidity_above=0.0, liquidity_below=0.0,
                total_liquidity=0.0, spread_bps=0.0,
                is_anomalous=True,
            )

        # Volume-weighted typical prices
        typical = (df_window["high"] + df_window["low"] + df_window["close"]) / 3
        vol = df_window["volume"]

        # Split volume above/below current price
        liq_above = float((vol[typical > current_price] * typical[typical > current_price]).sum())
        liq_below = float((vol[typical <= current_price] * typical[typical <= current_price]).sum())

        total = liq_above + liq_below

        if total > 0:
            raw_score = (liq_above - liq_below) / total
        else:
            raw_score = 0.0

        if total > 0 and abs(liq_above - liq_below) / total < self.min_ratio:
            raw_score = 0.0

        self._smoothed_bias = (
            self.ema_alpha * raw_score
            + (1 - self.ema_alpha) * self._smoothed_bias
        )
        score = max(-1.0, min(1.0, self._smoothed_bias))

        # Estimate spread from candle range
        last = df_window.iloc[-1]
        spread_bps = 0.0
        if last["close"] > 0:
            spread_bps = (last["high"] - last["low"]) / last["close"] * 10000 * 0.1

        return LiquidityBias(
            score=score,
            raw_score=raw_score,
            liquidity_above=liq_above,
            liquidity_below=liq_below,
            total_liquidity=total,
            spread_bps=spread_bps,
            is_anomalous=False,
        )

    def compute_direct(
        self,
        liq_above: float,
        liq_below: float,
        spread_bps: float,
    ) -> LiquidityBias:
        """
        Direct computation from pre-calculated liquidity values.
        """
        total = liq_above + liq_below

        if total > 0:
            raw_score = (liq_above - liq_below) / total
        else:
            raw_score = 0.0

        if total > 0 and abs(liq_above - liq_below) / total < self.min_ratio:
            raw_score = 0.0

        self._smoothed_bias = (
            self.ema_alpha * raw_score
            + (1 - self.ema_alpha) * self._smoothed_bias
        )
        score = max(-1.0, min(1.0, self._smoothed_bias))

        return LiquidityBias(
            score=score,
            raw_score=raw_score,
            liquidity_above=liq_above,
            liquidity_below=liq_below,
            total_liquidity=total,
            spread_bps=spread_bps,
            is_anomalous=False,
        )

    def reset(self) -> None:
        self._smoothed_bias = 0.0
