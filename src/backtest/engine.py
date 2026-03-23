"""
Event-driven backtesting engine with realistic fill simulation.
Processes OHLCV data candle-by-candle, simulates grid order fills,
stop-loss triggers, and tracks full P&L with slippage and fees.
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.config import Config
from src.heatmap.engine import BacktestHeatmap, LiquidityBias
from src.indicators.core import compute_all
from src.risk.manager import (
    DrawdownMonitor,
    compute_position_size,
    compute_stop_loss,
)
from src.strategy.grid import AdaptiveGrid, GridLevel, GridState
from src.strategy.regime import RegimeDetector, RegimeState
from src.strategy.signals import SignalPipeline, Signal

logger = logging.getLogger(__name__)


# ? Trade Record ?

@dataclass
class Trade:
    """Record of a single fill."""
    timestamp: pd.Timestamp
    side: str              # "BUY" or "SELL"
    price: float           # Fill price (with slippage)
    size: float            # Base asset size
    fee: float             # Fee in USD
    pnl: float = 0.0      # Realized P&L (set when position closes)
    entry_price: float = 0.0  # Entry price for this trade
    exit_price: float = 0.0   # Exit price (if closing)
    is_stop: bool = False     # True if triggered by stop-loss

    @property
    def value_usd(self) -> float:
        return self.price * self.size


# ? Position Tracker ?

@dataclass
class Position:
    """Tracks current open position for a market."""
    side: str = ""           # "LONG", "SHORT", or ""
    size: float = 0.0        # Absolute base size
    avg_entry: float = 0.0   # Average entry price
    unrealized_pnl: float = 0.0
    stop_loss: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.size > 0

    def update_pnl(self, current_price: float) -> float:
        """Update unrealized P&L."""
        if not self.is_open:
            self.unrealized_pnl = 0.0
            return 0.0

        if self.side == "LONG":
            self.unrealized_pnl = (current_price - self.avg_entry) * self.size
        else:
            self.unrealized_pnl = (self.avg_entry - current_price) * self.size
        return self.unrealized_pnl


# ? Backtest Engine ?

@dataclass
class BacktestResult:
    """Complete backtest results."""
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return_pct: float
    trades: list[Trade]
    equity_curve: pd.Series
    drawdown_curve: pd.Series
    # Metrics
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_duration: int = 0  # candles
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_trade_pnl: float = 0.0
    calmar_ratio: float = 0.0
    sortino_ratio: float = 0.0
    total_fees: float = 0.0
    config_params: dict[str, Any] = field(default_factory=dict)


class BacktestEngine:
    """
    Event-driven backtest engine.

    Flow per candle:
    1. Check stop-losses against high/low
    2. Check grid fill against high/low
    3. Compute indicators on updated data
    4. Evaluate signals
    5. Rebalance grid if needed
    6. Update drawdown monitor
    """

    def __init__(self, config: Config):
        self.config = config
        self.slippage_bps = config.backtest.slippage_bps
        self.maker_fee = config.fees.maker_fee_pct / 100
        self.taker_fee = config.fees.taker_fee_pct / 100

    def run(
        self,
        df: pd.DataFrame,
        symbol: str = "UNKNOWN",
        indicators: pd.DataFrame | None = None,
        verbose: bool = False,
    ) -> BacktestResult:
        """
        Run backtest on a OHLCV DataFrame.
        
        Args:
            df: OHLCV data
            symbol: Trading symbol
            indicators: Pre-calculated indicators (optional)
            verbose: Print progress (deprecated in new NumPy loop)
        """
        cfg = self.config
        capital = cfg.backtest.initial_capital
        initial_capital = capital

        # Market Maker logic components
        grid_gen = AdaptiveGrid(
            num_levels=cfg.grid.levels,
            spacing_atr_mult=cfg.grid.spacing_atr_mult,
            rebalance_threshold=cfg.grid.rebalance_threshold,
        )
        regime_det = RegimeDetector(
            trend_threshold=cfg.indicators.adx_trend_threshold,
        )
        signal_pipe = SignalPipeline(config=cfg.indicators)
        heatmap = BacktestHeatmap(
            ema_smoothing=cfg.heatmap.ema_smoothing,
            min_ratio=cfg.heatmap.min_liquidity_ratio,
        )
        dd_monitor = DrawdownMonitor(
            max_daily_drawdown_pct=cfg.risk.max_daily_drawdown_pct,
        )

        # Compute or use pre-calculated indicators
        if indicators is not None:
            inds = indicators
        else:
            inds = compute_all(
                df,
                rsi_period=cfg.indicators.rsi_period,
                adx_period=cfg.indicators.adx_period,
                atr_period=cfg.indicators.atr_period,
                momentum_period=cfg.indicators.momentum_period,
                vwap_session_hours=cfg.indicators.vwap_session_hours,
            )

        # State
        position = Position()
        trades: list[Trade] = []
        equity: list[float] = []
        total_fees = 0.0
        current_grid: GridState | None = None

        # Initialize drawdown monitor
        dd_monitor.initialize(capital, timestamp=0)

        # Skip warmup period (need indicators to populate)
        warmup = max(
            cfg.indicators.rsi_period,
            cfg.indicators.adx_period,
            cfg.indicators.atr_period,
            cfg.indicators.momentum_period,
        ) + 5

        heatmap_lookback = min(
            cfg.heatmap.rolling_window_minutes,
            len(df),
        )

        # Precompute lookups for speed
        ts_index = inds.index
        highs = inds["high"].values
        lows = inds["low"].values
        closes = inds["close"].values
        vols = inds["volume"].values
        atrs = inds["atr"].values if "atr" in inds else np.zeros(len(inds))
        adxs = inds["adx"].values if "adx" in inds else np.zeros(len(inds))
        plus_dis = inds["plus_di"].values if "plus_di" in inds else np.zeros(len(inds))
        minus_dis = inds["minus_di"].values if "minus_di" in inds else np.zeros(len(inds))

        # Typical prices for heatmap
        typical_prices = (highs + lows + closes) / 3.0
        weighted_vols = vols * typical_prices

        # Prepare signal processing inputs using precomputed numpy arrays
        rsi_vals = inds["rsi"].values if "rsi" in inds else np.full(len(inds), 50.0)
        vwap_dists = inds["vwap_distance"].values if "vwap_distance" in inds else np.zeros(len(inds))
        momentum_vals = inds["momentum"].values if "momentum" in inds else np.zeros(len(inds))

        for i in range(len(inds)):
            ts = ts_index[i]
            
            # Skip warmup
            if i < warmup:
                equity.append(capital)
                continue

            # Check if halted
            if dd_monitor.state.is_halted:
                # Close any open position
                if position.is_open:
                    close_price = closes[i]
                    pnl = self._close_position(
                        position, close_price, "HALT"
                    )
                    fee = abs(position.size * close_price) * self.taker_fee
                    total_fees += fee
                    capital += pnl - fee
                    trades.append(Trade(
                        timestamp=ts, side="CLOSE", price=close_price,
                        size=position.size, fee=fee, pnl=pnl,
                        entry_price=position.avg_entry,
                        exit_price=close_price,
                    ))
                    position = Position()
                    current_grid = None
                equity.append(capital)
                continue

            close = closes[i]
            high = highs[i]
            low = lows[i]

            # ? 3. Compute heatmap bias ?
            hm_start = max(0, i - heatmap_lookback)
            
            # Use precomputed typical prices and weighted volumes for speed
            window_typical = typical_prices[hm_start:i + 1]
            window_weighted = weighted_vols[hm_start:i + 1]
            
            mask_above = window_typical > close
            liq_above = float(np.sum(window_weighted[mask_above]))
            liq_below = float(np.sum(window_weighted[~mask_above]))
            
            spread_bps = 0.0
            if close > 0:
                spread_bps = (high - low) / close * 10000 * 0.1

            bias = heatmap.compute_direct(liq_above, liq_below, spread_bps)

            # ? 4. Evaluate regime + signals ?
            regime = regime_det.detect(
                adx_value=adxs[i],
                plus_di=plus_dis[i],
                minus_di=minus_dis[i],
            )
            
            # Pass a minimal dict to SignalPipeline to satisfy get() calls without df/dict overhead
            signal_row = {
                "vwap_distance": vwap_dists[i],
                "rsi": rsi_vals[i],
                "momentum": momentum_vals[i]
            }
            signal = signal_pipe.evaluate(signal_row, bias, regime, include_reasons=False)
            atr_val = atrs[i]

            # ? 1. Check stop-losses ?
            if position.is_open and position.stop_loss > 0:
                triggered = False
                if position.side == "LONG" and low <= position.stop_loss:
                    triggered = True
                    fill_price = position.stop_loss
                elif position.side == "SHORT" and high >= position.stop_loss:
                    triggered = True
                    fill_price = position.stop_loss

                if triggered:
                    # Apply slippage (adverse)
                    fill_price = self._apply_slippage(
                        fill_price, "SELL" if position.side == "LONG" else "BUY"
                    )
                    pnl = self._close_position(position, fill_price, "STOP")
                    fee = abs(position.size * fill_price) * self.taker_fee
                    total_fees += fee
                    capital += pnl - fee
                    trades.append(Trade(
                        timestamp=ts, side="CLOSE", price=fill_price,
                        size=position.size, fee=fee, pnl=pnl,
                        entry_price=position.avg_entry,
                        exit_price=fill_price, is_stop=True,
                    ))
                    dd_monitor.record_trade(pnl - fee)
                    position = Position()
                    current_grid = None

            # ? 2. Check grid fills ?
            if current_grid is not None:
                for level in current_grid.levels:
                    if level.is_filled:
                        continue
                    if level.size <= 0:
                        continue

                    # Check if price crossed grid level
                    filled = False
                    if level.side == "BUY" and low <= level.price:
                        filled = True
                    elif level.side == "SELL" and high >= level.price:
                        filled = True

                    if filled:
                        fill_price = self._apply_slippage(
                            level.price, level.side
                        )
                        fee = abs(level.size * fill_price) * self.maker_fee
                        total_fees += fee
                        capital -= fee

                        # Determine target side
                        target_side = "LONG" if level.side == "BUY" else "SHORT"

                        # If this fill is OPPOSITE to current position ? realize P&L
                        realized_pnl = 0.0
                        if position.is_open and position.side != target_side:
                            close_size = min(level.size, position.size)
                            if position.side == "LONG":
                                realized_pnl = (fill_price - position.avg_entry) * close_size
                            else:
                                realized_pnl = (position.avg_entry - fill_price) * close_size
                            capital += realized_pnl
                            dd_monitor.record_trade(realized_pnl - fee)

                        # Update position
                        self._add_to_position(
                            position, target_side, level.size,
                            fill_price, level.stop_loss,
                        )

                        level.is_filled = True
                        level.fill_price = fill_price

                        trades.append(Trade(
                            timestamp=ts, side=level.side,
                            price=fill_price, size=level.size,
                            fee=fee, pnl=realized_pnl,
                            entry_price=position.avg_entry if realized_pnl == 0 else fill_price,
                            exit_price=fill_price if realized_pnl != 0 else 0.0,
                        ))

            # ? 2b. Take-profit check ?
            if position.is_open and current_grid is not None:
                if cfg.market_maker.fixed_tp_bps > 0:
                    tp_distance = close * (cfg.market_maker.fixed_tp_bps / 10000)
                else:
                    tp_distance = atr_val * cfg.market_maker.tp_atr_mult
                if position.side == "LONG":
                    tp_price = position.avg_entry + tp_distance
                    if high >= tp_price:
                        fill_price = self._apply_slippage(tp_price, "SELL")
                        pnl = (fill_price - position.avg_entry) * position.size
                        fee = abs(position.size * fill_price) * self.taker_fee
                        total_fees += fee
                        capital += pnl - fee
                        trades.append(Trade(
                            timestamp=ts, side="CLOSE", price=fill_price,
                            size=position.size, fee=fee, pnl=pnl,
                            entry_price=position.avg_entry,
                            exit_price=fill_price,
                        ))
                        dd_monitor.record_trade(pnl - fee)
                        position = Position()
                        current_grid = None
                elif position.side == "SHORT":
                    tp_price = position.avg_entry - tp_distance
                    if low <= tp_price:
                        fill_price = self._apply_slippage(tp_price, "BUY")
                        pnl = (position.avg_entry - fill_price) * position.size
                        fee = abs(position.size * fill_price) * self.taker_fee
                        total_fees += fee
                        capital += pnl - fee
                        trades.append(Trade(
                            timestamp=ts, side="CLOSE", price=fill_price,
                            size=position.size, fee=fee, pnl=pnl,
                            entry_price=position.avg_entry,
                            exit_price=fill_price,
                        ))
                        dd_monitor.record_trade(pnl - fee)
                        position = Position()
                        current_grid = None

            # ? 5. Update unrealized P&L ?
            if position.is_open:
                position.update_pnl(close)

            # ? 6. Rebalance grid if needed ?
            should_rebalance = (
                current_grid is None
                or grid_gen.needs_rebalance(close)
            )

            if should_rebalance and not signal.is_neutral:
                # Position sizing
                stop_mult = (
                    cfg.risk.stop_atr_mult_trend
                    if regime.is_trend
                    else cfg.risk.stop_atr_mult_range
                )
                sizing = compute_position_size(
                    capital=capital,
                    risk_per_trade_pct=cfg.risk.risk_per_trade_pct,
                    atr_value=atr_val,
                    stop_atr_mult=stop_mult,
                    current_price=close,
                    max_position_pct=cfg.risk.max_position_pct,
                )

                # Apply signal weights
                base_size = sizing.size_base / cfg.grid.levels  # Per level

                current_grid = grid_gen.generate(
                    mid_price=close,
                    atr_value=atr_val,
                    bias_score=bias.score if not bias.is_anomalous else 0.0,
                    regime=regime.regime,
                    base_size=base_size,
                    stop_atr_mult=stop_mult,
                )

                # Apply signal filter: remove blocked sides
                if not signal.allow_long:
                    for lvl in current_grid.levels:
                        if lvl.side == "BUY":
                            lvl.size = 0
                if not signal.allow_short:
                    for lvl in current_grid.levels:
                        if lvl.side == "SELL":
                            lvl.size = 0

            # ? 7. Update drawdown ?
            total_value = capital + position.unrealized_pnl
            dd_monitor.update(total_value, timestamp=i)

            equity.append(total_value)

        # ? Close any remaining position ?
        if position.is_open and len(inds) > 0:
            final_price = inds.iloc[-1]["close"]
            pnl = self._close_position(position, final_price, "END")
            fee = abs(position.size * final_price) * self.taker_fee
            total_fees += fee
            capital += pnl - fee
            trades.append(Trade(
                timestamp=inds.index[-1], side="CLOSE",
                price=final_price, size=position.size,
                fee=fee, pnl=pnl,
                entry_price=position.avg_entry,
                exit_price=final_price,
            ))

        # ? Compute metrics ?
        equity_series = pd.Series(equity, index=inds.index[:len(equity)])
        result = self._compute_metrics(
            equity_series=equity_series,
            trades=trades,
            initial_capital=initial_capital,
            symbol=symbol,
            timeframe=self.config.timeframe,
            total_fees=total_fees,
        )
        return result

    def _apply_slippage(self, price: float, side: str) -> float:
        """Apply simulated slippage (adverse direction)."""
        slip = price * (self.slippage_bps / 10000)
        if side == "BUY":
            return price + slip  # Pay more when buying
        else:
            return price - slip  # Receive less when selling

    def _close_position(
        self, position: Position, close_price: float, reason: str
    ) -> float:
        """Close position and return realized P&L."""
        if position.side == "LONG":
            pnl = (close_price - position.avg_entry) * position.size
        else:
            pnl = (position.avg_entry - close_price) * position.size
        logger.debug(
            "Close %s position: entry=%.4f exit=%.4f pnl=%.4f reason=%s",
            position.side, position.avg_entry, close_price, pnl, reason,
        )
        return pnl

    def _add_to_position(
        self, position: Position, side: str, size: float,
        price: float, stop: float,
    ) -> None:
        """Add to or create a position."""
        if size <= 0:
            return

        if not position.is_open:
            position.side = side
            position.size = size
            position.avg_entry = price
            position.stop_loss = stop
        elif position.side == side:
            # Add to existing ? weighted average entry
            total_size = position.size + size
            position.avg_entry = (
                (position.avg_entry * position.size + price * size) / total_size
            )
            position.size = total_size
            # Keep the tightest stop
            if side == "LONG":
                position.stop_loss = max(position.stop_loss, stop)
            else:
                position.stop_loss = min(position.stop_loss, stop)
        else:
            # Opposite side ? close or reduce
            if size >= position.size:
                # Fully close + reverse
                position.side = side
                position.size = size - position.size
                position.avg_entry = price
                position.stop_loss = stop
            else:
                position.size -= size

    def _compute_metrics(
        self,
        equity_series: pd.Series,
        trades: list[Trade],
        initial_capital: float,
        symbol: str,
        timeframe: str,
        total_fees: float,
    ) -> BacktestResult:
        """Compute all performance metrics."""
        final_capital = equity_series.iloc[-1] if len(equity_series) > 0 else initial_capital
        total_return = (final_capital - initial_capital) / initial_capital * 100

        # Drawdown curve
        peak = equity_series.cummax()
        drawdown = (peak - equity_series) / peak * 100
        max_dd = drawdown.max() if len(drawdown) > 0 else 0.0

        # Max drawdown duration
        is_dd = drawdown > 0
        dd_groups = (~is_dd).cumsum()
        dd_durations = is_dd.groupby(dd_groups).sum()
        max_dd_duration = int(dd_durations.max()) if len(dd_durations) > 0 else 0

        # Trade metrics
        closed_trades = [t for t in trades if t.pnl != 0 or t.side == "CLOSE"]
        total_trades = len(closed_trades)

        winning = [t for t in closed_trades if t.pnl > 0]
        losing = [t for t in closed_trades if t.pnl < 0]

        win_rate = len(winning) / total_trades * 100 if total_trades > 0 else 0
        avg_pnl = sum(t.pnl for t in closed_trades) / total_trades if total_trades > 0 else 0

        gross_profit = sum(t.pnl for t in winning) if winning else 0
        gross_loss = abs(float(sum(t.pnl for t in losing))) if losing else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Returns for Sharpe/Sortino
        returns = equity_series.pct_change().dropna()

        # Sharpe ratio (annualized)
        if len(returns) > 1 and returns.std() > 0:
            # Annualization factor: depends on timeframe
            tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
            minutes = tf_minutes.get(timeframe, 5)
            periods_per_year = 365 * 24 * 60 / minutes
            sharpe = returns.mean() / returns.std() * np.sqrt(periods_per_year)
        else:
            sharpe = 0.0

        # Sortino ratio
        neg_returns = returns[returns < 0]
        if len(neg_returns) > 1 and neg_returns.std() > 0:
            sortino = returns.mean() / neg_returns.std() * np.sqrt(periods_per_year)
        else:
            sortino = 0.0

        # Calmar ratio
        calmar = total_return / max_dd if max_dd > 0 else 0.0

        start_date = str(equity_series.index[0]) if len(equity_series) > 0 else ""
        end_date = str(equity_series.index[-1]) if len(equity_series) > 0 else ""

        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            final_capital=round(float(final_capital), 2),
            total_return_pct=round(float(total_return), 2),
            trades=trades,
            equity_curve=equity_series,
            drawdown_curve=drawdown,
            sharpe_ratio=round(float(sharpe), 3),
            max_drawdown_pct=round(float(max_dd), 2),
            max_drawdown_duration=max_dd_duration,
            win_rate=round(float(win_rate), 1),
            profit_factor=round(float(profit_factor), 3),
            total_trades=total_trades,
            winning_trades=len(winning),
            losing_trades=len(losing),
            avg_trade_pnl=round(float(avg_pnl), 4),
            calmar_ratio=round(float(calmar), 3),
            sortino_ratio=round(float(sortino), 3),
            total_fees=round(float(total_fees), 4),
        )
