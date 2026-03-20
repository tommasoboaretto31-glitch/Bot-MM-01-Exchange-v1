""" Live trading module for real-time execution (Paper Mode - Market Maker). """

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd # type: ignore
import numpy as np # type: ignore

from src.api.client import O1Client # type: ignore
from src.config import Config # type: ignore
from src.data.candles import CandleAggregator, Candle # type: ignore
from src.indicators.core import compute_all # type: ignore
from src.data.binance import BinanceDataClient, o1_to_binance # type: ignore
from src.risk.manager import DrawdownMonitor # type: ignore

from src.dashboard.app import update_state, update_volume
from src.strategy.signals import SignalPipeline, Signal
from src.strategy.regime import RegimeDetector, RegimeState
from src.heatmap.engine import BacktestHeatmap, LiquidityBias

logger = logging.getLogger(__name__)

class MMSymbolState:
    """Tracks paper trading state per symbol for the Market Maker."""
    def __init__(self):
        self.inventory: float = 0.0          # Positive = Long, Negative = Short
        self.avg_entry: float = 0.0
        self.candles_in_position: int = 0
        
        self.buy_price: float = 0.0
        self.buy_size: float = 0.0
        self.buy_order_id: int = 0
        self.sell_price: float = 0.0
        self.sell_size: float = 0.0
        self.sell_order_id: int = 0
        
        # P&L tracking
        self.realized_pnl: float = 0.0
        self.fees_paid: float = 0.0
        self.volume: float = 0.0
        self.trades_count: int = 0
        
        # Smart caching
        self.last_atr: float = 0.0
        self.last_smart_score: float = 0.0
        self.last_requote_time: float = 0.0
        
        # Extended Strategy Data
        self.last_signal: Optional[Signal] = None
        self.last_regime: Optional[RegimeState] = None
        self.heatmap_engine = BacktestHeatmap()

class LiveTrader:
    """
    Manages real-time paper trading specifically for Market Making.
    Connects to WebSocket, aggregates candles, and executes strategy.
    """

    def __init__(self, config: Config, client: O1Client | None = None, allocation_weight: float = 1.0):
        self.config = config
        self.allocation_weight = allocation_weight
        
        # Use shared client if provided, otherwise create new one
        if client:
            self.client = client
        else:
            kp = None if config.paper_mode else config.keypair_path
            self.client = O1Client(config.api_url, keypair_path=kp)
        
        # Market state: symbol -> aggregator
        self.aggregators: Dict[str, CandleAggregator] = {}
        # Market Maker state per symbol
        self.mm_states: Dict[str, MMSymbolState] = {}
        # Map symbol -> market_id for REST polling
        self.market_ids: Dict[str, int] = {}
        self.last_trade_times: Dict[str, int] = {}
        self.market_decimals: Dict[str, tuple[int, int]] = {} # (price_dec, size_dec)
        
        # External clients
        self.binance: BinanceDataClient = BinanceDataClient()
        self.drawdown_monitor = DrawdownMonitor(config.risk.max_daily_drawdown_pct)
        
        # Paper trading global state
        self.initial_balance = config.capital
        self.balance = config.capital
        self.trades_today = 0
        self.halted = False
        
        # Strategy Pipeline
        self.signal_pipeline = SignalPipeline(config.indicators)
        self.regime_detectors: Dict[str, RegimeDetector] = {
            s: RegimeDetector(
                trend_threshold=config.indicators.adx_trend_threshold
            ) for s in config.active_symbols
        }

    async def start(self):
        """Initialize connections and start the trading loop."""
        
        if not self.config.paper_mode:
            try:
                # Fetch real initial balance for dashboard ROI tracking
                pubkey = self.client.user_pubkey_b58
                user_info = await self.client.get_user(pubkey)
                acc_id = user_info.get("accountIds", [0])[0]
                acc_info = await self.client.get_account(acc_id)
                real_bal = 0.0
                for bal in acc_info.get("balances", []):
                    if bal.get("tokenId") == 0:
                        real_bal = float(bal.get("amount", 0))
                        break
                if real_bal > 0:
                    self.initial_balance = real_bal
                    self.balance = real_bal
                    msg = f"Mainnet balance detected: {real_bal:.2f} USDC"
                    logger.info(msg)
                    # Push initial log to dashboard
                    update_state(log_msg=msg, paper_mode=False)
            except Exception as e:
                logger.error(f"Failed to fetch initial balance: {e}")
                # Fallback to config capital if fetching fails
                self.initial_balance = self.config.capital
                self.balance = self.config.capital

        # Initialize global drawdown monitor
        self.drawdown_monitor.initialize(self.initial_balance)

        mode_str = "PAPER MODE" if self.config.paper_mode else "LIVE MAINNET"
        logger.info(f"AUDIT - Starting MM LiveTrader in {mode_str}")
        logger.info(f"AUDIT - Capital: ${self.initial_balance}")
        logger.info(f"AUDIT - Active Symbols: {self.config.active_symbols}")
        
        # Push initial capital and status to dashboard immediately
        update_state(
            status="running",
            performance={
                "capital": self.initial_balance,
                "initial_capital": self.initial_balance,
                "pnl_today": 0.0
            },
            log_msg=f"Bot Engine Initialized with ${self.initial_balance} capital"
        )
        await self._update_dashboard()
        for symbol in self.config.active_symbols:
            try:
                mi = await self.client.market_by_symbol(symbol)
                if not mi:
                    logger.warning(f"Market info not found for {symbol}, skipping.")
                    continue
                    
                self.market_ids[symbol] = mi.market_id
                self.market_decimals[symbol] = (mi.price_decimals, mi.size_decimals) # type: ignore
                self.aggregators[symbol] = CandleAggregator(self.config.timeframe)
                self.mm_states[symbol] = MMSymbolState()
                self.last_trade_times[symbol] = 0
                logger.info(f"Initialized polling for {symbol} (Market ID: {mi.market_id})")
            except Exception as e:
                logger.error(f"Failed to initialize market {symbol}: {e}")
                continue
            
        # ? Pre-load historical candles from Binance in background to avoid blocking start ?
        asyncio.create_task(self._preload_candles())
            
        # Run Polling in the background
        asyncio.create_task(self._poll_trades_loop())
        
        # Main monitoring loop
        from src.dashboard.app import _shutdown_requested, _is_paused
        while not _shutdown_requested:
            if _is_paused:
                # Cancel all orders once when entering pause
                await self._cancel_all_orders()
                update_state(status="paused", log_msg="Trading paused by user. Monitoring only...")
                while _is_paused and not _shutdown_requested:
                    await self._update_dashboard()
                    await asyncio.sleep(5)
                if not _shutdown_requested:
                    update_state(status="running", log_msg="Trading resumed.")
                continue
                
            await self._update_dashboard()
            await asyncio.sleep(2)
            
        logger.info("STOP SIGNAL RECEIVED. Initiating graceful shutdown...")
        update_state(status="halting", log_msg="Graceful shutdown initiated...")
        
        # 1. Cancel all active orders for safety
        logger.info("Cleaning up active orders...")
        await self._cancel_all_orders()
            
        update_state(status="idle", log_msg="Bot Engine Stopped Safely.")
        logger.info("Bot Engine Stopped Safely.")

    async def _preload_candles(self):
        """Download recent candles from Binance concurrently."""
        tasks = []
        for symbol in list(self.aggregators.keys()):
            binance_sym = o1_to_binance(symbol)
            if binance_sym:
                tasks.append(self._preload_one_symbol(symbol, binance_sym))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _preload_one_symbol(self, symbol: str, binance_sym: str):
        try:
            df = await asyncio.to_thread(
                self.binance.fetch_klines_with_oi,
                binance_sym,
                self.config.timeframe,
                limit=50,
            )
            
            if df.empty:
                logger.warning(f"[{symbol}] No historical data found on Binance")
                return
            
            candles = []
            for ts, row in df.iterrows():
                candles.append(Candle(
                    timestamp=ts, # type: ignore
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"])
                ))
            
            self.aggregators[symbol].preload(candles)
            logger.info(f"[{symbol}] Pre-loaded {len(candles)} candles.")

        except Exception as e:
            logger.error(f"[{symbol}] Failed to preload candles: {e}")

    async def _sync_account_balance(self):
        """Fetch real account balance from exchange API in non-paper mode."""
        if self.config.paper_mode:
            return
            
        try:
            pubkey = self.client.user_pubkey_b58
            if not pubkey:
                return
            user_info = await self.client.get_user(pubkey)
            acc_id = user_info.get("accountIds", [0])[0]
            acc_info = await self.client.get_account(acc_id)
            for bal in acc_info.get("balances", []):
                if bal.get("tokenId") == 0:
                    self.balance = float(bal.get("amount", 0))
                    break
        except Exception as e:
            logger.error(f"Failed to sync account balance: {e}")

    async def _poll_trades_loop(self):
        """Periodically polls the REST /trades endpoint for all active markets."""
        while True:
            tasks = []
            for symbol, market_id in self.market_ids.items():
                tasks.append(self._poll_market(symbol, market_id))
            
            # Fetch all markets concurrently
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(1) # Faster poll mapping for volume trading

    async def _poll_market(self, symbol: str, market_id: int):
        try:
            res = await self.client.get_trades(market_id)
            trades = res.get("items", []) if isinstance(res, dict) else res
            if trades and isinstance(trades, list):
                trades.sort(key=lambda t: t.get("time", ""))
                await self._process_trades(symbol, trades)
        except Exception as e:
            logger.error(f"Error polling trades for {symbol}: {e}")

    async def _process_trades(self, symbol: str, trades: List[Dict]):
        """Process a batch of trades from the REST API."""
        new_trades = 0
        last_ts = self.last_trade_times[symbol]
        last_price = 0.0
        
        for trade in trades:
            time_str = trade.get("time", "")
            try:
                if time_str.endswith("Z"):
                    time_str = time_str[:-1] + "+00:00"
                dt = datetime.fromisoformat(time_str)
                ts_micro = int(dt.timestamp() * 1_000_000)
            except Exception as e:
                continue

            if ts_micro <= last_ts:
                continue
                
            self.last_trade_times[symbol] = ts_micro
            new_trades += 1
            
            price = float(trade["price"])
            last_price = price
            size_raw = trade.get("baseSize", trade.get("size", 0))
            if size_raw is None:
                size_raw = 0.0
            size = float(size_raw)
            
            completed_candle = self.aggregators[symbol].update(price, size, dt)
            if completed_candle:
                asyncio.create_task(self._on_candle_close(symbol))
            
            await self._check_paper_fills(symbol, price)
            await self._check_stop_loss(symbol, price)

        # Triggers a dynamic requote check if enough time passed and a trade occurred
        if new_trades > 0 and last_price > 0:
            now = time.time()
            state = self.mm_states[symbol]
            if now - state.last_requote_time > 2.0: # Max 1 requote every 2 seconds
                state.last_requote_time = now
                asyncio.create_task(self._update_market_maker_quotes(symbol, last_price))

    async def _on_candle_close(self, symbol: str):
        """Heavy duty: Recalculate indicators at candle close."""
        
        if not self.config.paper_mode:
            await self._sync_account_balance()
        
        # Drawdown Check
        dd_state = self.drawdown_monitor.update(self.balance)
        if dd_state.is_halted:
            if not self.halted:
                logger.error(f"DRAWDOWN BREAKER: {dd_state.halt_reason}. HALTING ALL TRADES.")
                self.halted = True
                await self._cancel_all_orders()
            return
        
        # User Pause Check
        from src.dashboard.app import _is_paused
        if _is_paused:
            return
        
        
        # ── Binance: sole data source for ALL indicators ─────────────────────
        binance_sym = o1_to_binance(symbol)
        df_bin = pd.DataFrame()
        oi_series = None
        if binance_sym:
            try:
                df_bin = await asyncio.to_thread(
                    self.binance.fetch_klines_with_oi, binance_sym, "5m", 100
                )
                if not df_bin.empty and "oi" in df_bin.columns:
                    oi_series = df_bin["oi"]
            except Exception as e:
                logger.debug(f"[{symbol}] Binance fetch failed: {e}")

        ind_cfg = self.config.indicators

        # Require valid Binance data — it is the single source of truth
        if df_bin.empty or len(df_bin) < max(ind_cfg.rsi_period, ind_cfg.atr_period):
            return

        bin_inds = compute_all(
            df_bin,
            rsi_period=ind_cfg.rsi_period,
            adx_period=ind_cfg.adx_period,
            atr_period=ind_cfg.atr_period,
            momentum_period=ind_cfg.momentum_period,
            vwap_session_hours=ind_cfg.vwap_session_hours,
            oi_series=oi_series,
        )
        bin_last = bin_inds.iloc[-1]

        cvd_div = float(bin_last.get("cvd_divergence", 0) or 0)
        rsi_div = float(bin_last.get("rsi_divergence", 0) or 0)
        oi_sig  = float(bin_last.get("oi_signal", 0) or 0)

        close   = float(bin_last["close"])
        atr_val = float(bin_last.get("atr", 0) or 0)

        if atr_val <= 0 or np.isnan(atr_val) or close <= 0:
            return

        state = self.mm_states[symbol]
        state.last_atr = atr_val
        state.last_smart_score = cvd_div + rsi_div + oi_sig

        # 1. Regime Detection (ADX/DI from Binance)
        regime_detector = self.regime_detectors.get(symbol)
        if regime_detector:
            adx_val  = float(bin_last.get("adx", 20.0) or 20.0)
            plus_di  = float(bin_last.get("plus_di", 0.0) or 0.0)
            minus_di = float(bin_last.get("minus_di", 0.0) or 0.0)
            state.last_regime = regime_detector.detect(adx_val, plus_di, minus_di)

        # 2. Heatmap Bias — uses Binance OHLCV (richer volume data)
        candle_window = df_bin.iloc[-30:]
        current_bias = state.heatmap_engine.compute_from_candles(candle_window, close)

        # 3. Signal Pipeline (all filters operate on Binance indicators)
        state.last_signal = self.signal_pipeline.evaluate(
            indicators=bin_last,
            bias=current_bias,
            regime=state.last_regime or RegimeState("range", 20, 0, 0, "NONE", 0.5),
        )

        # Volatility Pause (ATR spike detection on Binance data)
        vol_pause_mult = getattr(self.config.market_maker, "volatility_pause_mult", 3.0)
        if vol_pause_mult > 0 and len(bin_inds) >= 28:
            avg_atr = bin_inds["atr"].iloc[-28:-1].mean()
            if avg_atr > 0 and atr_val > avg_atr * vol_pause_mult:
                logger.warning(
                    f"[{symbol}] VOLATILITY PAUSE: ATR {atr_val:.6f} > {vol_pause_mult}x avg. Halting."
                )
                if abs(state.inventory) > 0:
                    await self._close_position_at_market(symbol, close)
                return

        # Stale Position Killer
        mm_cfg = self.config.market_maker
        if abs(state.inventory) > 0:
            state.candles_in_position += 1
            if state.candles_in_position >= mm_cfg.stale_candles:
                logger.info(
                    f"[{symbol}] Closing STALE position of {state.inventory:.4f} @ {close:.4f}"
                )
                await self._close_position_at_market(symbol, close)

        # Trigger quote update using Binance reference close
        await self._update_market_maker_quotes(symbol, close)

    async def _update_market_maker_quotes(self, symbol: str, current_price: float):
        """Compute the dynamic grid around current spot price and update orders."""
        from src.dashboard.app import _is_paused
        if self.halted or _is_paused:
            return
            
        state = self.mm_states[symbol]
        atr_val = state.last_atr
        if atr_val <= 0:
            return
            
        mm_cfg = self.config.market_maker
        
        if mm_cfg.use_atr_spread:
            atr_spread_bps = (atr_val / current_price) * 10000
            spread_bps = max(atr_spread_bps * mm_cfg.atr_spread_mult, mm_cfg.min_spread_bps)
        else:
            spread_bps = mm_cfg.spread_bps

        half_spread = current_price * (spread_bps / 10000)

        price_dec, size_dec = self.market_decimals[symbol]
        factor = 10 ** size_dec
        min_size = 1.0 / factor
        price_factor = 10 ** price_dec
        min_tick = 1.0 / price_factor
        
        max_inv = self.balance * (mm_cfg.max_inventory_pct / 100)
        inventory_value = abs(state.inventory * current_price)
        available = max(0, max_inv - inventory_value)

        target_order_usd = self.balance * (mm_cfg.order_size_pct / 100)
        
        # ? NEW: Signal-Based Filtering & Weighting ?
        sig = state.last_signal
        buy_weight = 1.0
        sell_weight = 1.0
        
        if sig:
            if not sig.allow_long:
                logger.debug(f"[{symbol}] Signal blocking LONGS: {sig.reasons}")
                buy_weight = 0.0
            else:
                buy_weight = sig.long_weight
                
            if not sig.allow_short:
                logger.debug(f"[{symbol}] Signal blocking SHORTS: {sig.reasons}")
                sell_weight = 0.0
            else:
                sell_weight = sig.short_weight

        # Compute sizes with weights
        # Always use the most recent balance for dynamic sizing
        # Scale total balance by this trader's allocation weight
        current_balance = self.balance * self.allocation_weight
        target_order_usd = current_balance * (mm_cfg.order_size_pct / 100)
        
        buy_order_usd = min(target_order_usd * buy_weight, available if available > 0 else target_order_usd * 0.5)
        sell_order_usd = min(target_order_usd * sell_weight, available if available > 0 else target_order_usd * 0.5)
        
        buy_size = int((buy_order_usd / current_price) * factor) / factor if buy_order_usd > 0 else 0
        sell_size = int((sell_order_usd / current_price) * factor) / factor if sell_order_usd > 0 else 0
        
        if buy_size < min_size and sell_size < min_size:
            state.buy_size = 0
            state.sell_size = 0
            return

        ideal_buy = current_price - half_spread
        ideal_sell = current_price + half_spread
        
        # 1. Inventory Skew
        skew_factor = getattr(mm_cfg, 'inventory_skew_factor', 0.5)
        if skew_factor > 0 and abs(state.inventory) > 0 and max_inv > 0:
            inv_utilization = min(inventory_value / max_inv, 1.0)
            skew = half_spread * inv_utilization * skew_factor
            if state.inventory > 0:
                ideal_buy -= skew
                ideal_sell -= skew
            else:
                ideal_buy += skew
                ideal_sell += skew

        # 2. Smart Indicator Skew
        if state.last_smart_score != 0:
            smart_skew = half_spread * 0.2 * state.last_smart_score
            ideal_buy += smart_skew
            ideal_sell += smart_skew

        # 3. Fixed Take Profit Override
        fixed_tp = getattr(mm_cfg, 'fixed_tp_bps', 0)
        if fixed_tp > 0 and abs(state.inventory) > 0:
            tp_dist = state.avg_entry * (fixed_tp / 10000)
            if state.inventory > 0:
                ideal_tp_price = state.avg_entry + tp_dist
                ideal_sell = max(ideal_tp_price, current_price + (current_price * 0.0001))
            else:
                ideal_tp_price = state.avg_entry - tp_dist
                ideal_buy = min(ideal_tp_price, current_price - (current_price * 0.0001))

        # 4. Tick Alignment
        ideal_buy = int(ideal_buy * price_factor) / price_factor
        ideal_sell = int(ideal_sell * price_factor + 0.999999) / price_factor
        if ideal_sell <= ideal_buy:
            ideal_sell = ideal_buy + min_tick

        # Check if we need to replace existing orders
        needs_buy_requote = abs(ideal_buy - state.buy_price) > (min_tick * 2) or state.buy_size != buy_size
        needs_sell_requote = abs(ideal_sell - state.sell_price) > (min_tick * 2) or state.sell_size != sell_size
        
        if not (needs_buy_requote or needs_sell_requote):
            return # Quotes are still competitive
            
        state.buy_price = ideal_buy
        state.buy_size = buy_size if needs_buy_requote else state.buy_size
        state.sell_price = ideal_sell
        state.sell_size = sell_size if needs_sell_requote else state.sell_size

        logger.debug(f"[{symbol}] Re-Quoting B: {state.buy_price:.4f} (S:{buy_size}) A: {state.sell_price:.4f} (S:{sell_size})")

        if not self.config.paper_mode:
            cancels = []
            if needs_buy_requote and state.buy_order_id > 0:
                cancels.append(self.client.cancel_order(state.buy_order_id))
                state.buy_order_id = 0
            if needs_sell_requote and state.sell_order_id > 0:
                cancels.append(self.client.cancel_order(state.sell_order_id))
                state.sell_order_id = 0
                
            if cancels:
                # Cancel concurrently
                res = await asyncio.gather(*cancels, return_exceptions=True)
                for r in res:
                    if isinstance(r, BaseException):
                        logger.debug(f"[{symbol}] Cancel error: {r}")

            mid = self.market_ids[symbol]
            places = []
            if needs_buy_requote and state.buy_size > 0:
                places.append(self.client.place_order(mid, "BUY", state.buy_size, state.buy_price, "post_only"))
            if needs_sell_requote and state.sell_size > 0:
                places.append(self.client.place_order(mid, "SELL", state.sell_size, state.sell_price, "post_only"))

            if places:
                # Place concurrently
                results = await asyncio.gather(*places, return_exceptions=True)
                idx = 0
                if needs_buy_requote and state.buy_size > 0:
                    r = results[idx]
                    idx += 1
                    if isinstance(r, BaseException):
                        if "POST_ONLY" not in str(r) and "RISK" not in str(r):
                            logger.error(f"[{symbol}] Real BUY failed: {r}")
                    elif hasattr(r, 'order_id'):
                        state.buy_order_id = r.order_id
                        
                if needs_sell_requote and state.sell_size > 0:
                    r = results[idx]
                    if isinstance(r, BaseException):
                        if "POST_ONLY" not in str(r) and "RISK" not in str(r):
                            logger.error(f"[{symbol}] Real SELL failed: {r}")
                    elif hasattr(r, 'order_id'):
                        state.sell_order_id = r.order_id

    async def _close_position_at_market(self, symbol: str, price: float):
        """Force close an inventory position."""
        state = self.mm_states[symbol]
        if state.inventory == 0:
            return
            
        fee_pct = self.config.fees.taker_fee_pct / 100
        size = abs(state.inventory)
        
        if not self.config.paper_mode:
            cancels = []
            if getattr(state, 'buy_order_id', 0) > 0:
                cancels.append(self.client.cancel_order(state.buy_order_id))
                state.buy_order_id = 0
            if getattr(state, 'sell_order_id', 0) > 0:
                cancels.append(self.client.cancel_order(state.sell_order_id))
                state.sell_order_id = 0
            
            if cancels:
                await asyncio.gather(*cancels, return_exceptions=True)
                await asyncio.sleep(0.3)
                
            price_dec, size_dec = self.market_decimals[symbol]
            factor = 10 ** size_dec
            price_factor = 10 ** price_dec
            size = int(size * factor) / factor
            
            side = "SELL" if state.inventory > 0 else "BUY"
            slippage = 0.05
            raw_limit = price * (1 - slippage) if side == "SELL" else price * (1 + slippage)
            limit_price = int(raw_limit * price_factor) / price_factor
            
            if size > 0:
                try:
                    await self.client.place_order(self.market_ids[symbol], side, size, limit_price, "immediate", reduce_only=True)
                    logger.info(f"[{symbol}] Sent direct REAL IOC {side} to close stale position.")
                except Exception as e:
                    logger.error(f"[{symbol}] Failed to send real IOC close: {e}", exc_info=True)
        
        # PnL processing
        if state.inventory > 0:
            pnl = (price - state.avg_entry) * size
        else:
            pnl = (state.avg_entry - price) * size
            
        fee = size * price * fee_pct
        
        state.realized_pnl += pnl
        state.fees_paid += fee
        self.balance += (pnl - fee)
        
        trade_vol = size * price
        state.volume += trade_vol
        state.trades_count += 1
        self.trades_today += 1
        
        update_volume(symbol, trade_vol, pnl, fee)
        logger.info(f"[{symbol}] Market Closed {size:.4f} @ {price:.4f} PNL: ${pnl:.2f} Fee: ${fee:.4f}")
        
        state.inventory = 0.0
        state.avg_entry = 0.0
        state.candles_in_position = 0

    async def _check_stop_loss(self, symbol: str, current_price: float):
        state = self.mm_states[symbol]
        if state.inventory == 0:
            return

        # Dynamic Stop-Loss logic
        base_stop_bps = getattr(self.config.market_maker, 'stop_loss_bps', 35)
        stop_atr_mult = getattr(self.config.risk, 'stop_atr_mult_range', 2.0)
        
        # Calculate ATR-based stop in BPS
        if state.last_atr > 0:
            atr_bps = (state.last_atr * stop_atr_mult / state.avg_entry) * 10000
            # Use the wider of the two: base or ATR-based
            stop_bps = max(base_stop_bps, atr_bps)
        else:
            stop_bps = base_stop_bps

        if state.inventory > 0:
            adverse_bps = ((state.avg_entry - current_price) / state.avg_entry) * 10000
        else:
            adverse_bps = ((current_price - state.avg_entry) / state.avg_entry) * 10000

        if adverse_bps >= stop_bps:
            logger.warning(f"[{symbol}] STOP-LOSS TRIGGERED: {adverse_bps:.1f} bps move (Threshold: {stop_bps:.1f} bps). Closing.")
            
            if not self.config.paper_mode:
                cancels = []
                for oid in [state.buy_order_id, state.sell_order_id]:
                    if oid > 0:
                        cancels.append(self.client.cancel_order(oid))
                if cancels:
                    await asyncio.gather(*cancels, return_exceptions=True)
                state.buy_order_id = 0
                state.sell_order_id = 0

            await self._close_position_at_market(symbol, current_price)

    async def _check_paper_fills(self, symbol: str, current_price: float):
        state = self.mm_states[symbol]
        if state.buy_size <= 0 and state.sell_size <= 0:
            return
            
        fee_pct = self.config.fees.maker_fee_pct / 100
        slippage = self.config.backtest.slippage_bps / 10000

        if state.buy_size > 0 and current_price <= state.buy_price:
            fill_price = state.buy_price * (1 + slippage)
            self._execute_fill(symbol, "BUY", fill_price, state.buy_size, fee_pct)
            state.buy_size = 0
            state.candles_in_position = 0

        elif state.sell_size > 0 and current_price >= state.sell_price:
            fill_price = state.sell_price * (1 - slippage)
            self._execute_fill(symbol, "SELL", fill_price, state.sell_size, fee_pct)
            state.sell_size = 0
            state.candles_in_position = 0

    def _execute_fill(self, symbol: str, side: str, price: float, size: float, fee_pct: float):
        state = self.mm_states[symbol]
        fee = size * price * fee_pct
        trade_vol = size * price
        
        state.fees_paid += fee
        self.balance -= fee
        state.volume += trade_vol
        state.trades_count += 1
        self.trades_today += 1
        
        pnl = 0.0

        if side == "BUY":
            if state.inventory < 0:
                close_size = min(size, abs(state.inventory))
                pnl = (state.avg_entry - price) * close_size
                state.realized_pnl += pnl
                self.balance += pnl
                
                remaining = size - close_size
                if close_size >= abs(state.inventory):
                    state.inventory = remaining
                    state.avg_entry = price if remaining > 0 else 0
                else:
                    state.inventory += size
            else:
                total_inv = state.inventory + size
                state.avg_entry = (state.avg_entry * state.inventory + price * size) / total_inv
                state.inventory = total_inv
                
            logger.info(f"[{symbol}] MM BUY Fill {size:.4f} @ {price:.4f} PNL: ${pnl:.2f}")
            update_volume(symbol, trade_vol, pnl, fee)

        elif side == "SELL":
            if state.inventory > 0:
                close_size = min(size, state.inventory)
                pnl = (price - state.avg_entry) * close_size
                state.realized_pnl += pnl
                self.balance += pnl
                
                remaining = size - close_size
                if close_size >= state.inventory:
                    state.inventory = -remaining
                    state.avg_entry = price if remaining > 0 else 0
                else:
                    state.inventory -= size
            else:
                total_inv = abs(state.inventory) + size
                state.avg_entry = (state.avg_entry * abs(state.inventory) + price * size) / total_inv
                state.inventory = -total_inv
                
            logger.info(f"[{symbol}] MM SELL Fill {size:.4f} @ {price:.4f} PNL: ${pnl:.2f}")
            update_volume(symbol, trade_vol, pnl, fee)

    async def _cancel_all_orders(self):
        """Emergency method to cancel all known active orders."""
        cancels = []
        for sym, state in self.mm_states.items():
            if state.buy_order_id > 0:
                cancels.append(self.client.cancel_order(state.buy_order_id))
                state.buy_order_id = 0
            if state.sell_order_id > 0:
                cancels.append(self.client.cancel_order(state.sell_order_id))
                state.sell_order_id = 0
        if cancels:
            await asyncio.gather(*cancels, return_exceptions=True)

    async def _update_dashboard(self):
        """Push internal state to the dashboard API."""
        # Use local self.balance instead of API pull to avoid double-counting Unrealized PnL
        # since the exchange API's 'amount' represents total account equity.
        
        unrealized = 0.0
        positions_list = []
        
        for sym, state in self.mm_states.items():
            if abs(state.inventory) > 0:
                df = self.aggregators[sym].to_df()
                if not df.empty:
                    current_price = df.iloc[-1]["close"]
                    if state.inventory > 0:
                        upnl = (current_price - state.avg_entry) * state.inventory
                    else:
                        upnl = (state.avg_entry - current_price) * abs(state.inventory)
                    
                    unrealized += upnl
                    
                    positions_list.append({
                        "symbol": sym,
                        "side": "LONG" if state.inventory > 0 else "SHORT",
                        "size": abs(state.inventory),
                        "entry": state.avg_entry,
                        "mark": current_price,
                        "pnl": upnl,
                        "stop": 0.0
                    })

        total_value = self.balance + unrealized
        total_volume = sum(state.volume for state in self.mm_states.values())

        perf = {
            "capital": total_value,
            "initial_capital": self.initial_balance,
            "pnl_today": total_value - self.initial_balance,
            "total_return_pct": ((total_value / self.initial_balance) - 1) * 100 if self.initial_balance else 0,
            "trades_today": self.trades_today,
            "volume": total_volume,
            "api_calls_total": self.client.stats["api_calls_total"],
            "api_calls_failed": self.client.stats["api_calls_failed"],
            "orders_placed": self.client.stats["orders_placed"],
            "orders_cancelled": self.client.stats["orders_cancelled"],
        }
        
        sig_info = { "regime": "market_making", "bias_score": 0.0 }
        
        state_str = "halted" if self.halted else "running"
        update_state(
            status=state_str,
            performance=perf,
            signal=sig_info,
            positions=positions_list,
            paper_mode=self.config.paper_mode
        )

if __name__ == "__main__":
    from src.config import load_config # type: ignore
    cfg = load_config()
    trader = LiveTrader(cfg)
    
    # Graceful shutdown logic can be added here
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(trader.start())
    except KeyboardInterrupt:
        logger.info("Bot manually interrupted.")
    finally:
        loop.run_until_complete(trader.client.close())
