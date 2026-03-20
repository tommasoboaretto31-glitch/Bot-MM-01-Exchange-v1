""" Multi-market runner for ZeroOne Bot. 
Shares a single O1Client session across multiple traders to avoid conflicts.
"""

import asyncio
import logging
import sys
import os
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.api.client import O1Client
from src.config import load_config, load_active_config, load_coin_config
from src.live.trader import LiveTrader
from src.dashboard.app import update_state, _shutdown_requested, _is_paused

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MultiRunner")

async def run_multi_bot():
    """Entry point for multi-coin trading."""
    base_cfg = load_config()
    active_coins = load_active_config() # dict {symbol: weight}
    
    if not active_coins:
        logger.error("No active coins found in config/active.toml!")
        return

    # Initialize shared client
    kp = None if base_cfg.paper_mode else base_cfg.keypair_path
    client = O1Client(base_cfg.api_url, keypair_path=kp)
    
    try:
        # Pre-authenticate once to establish the session
        if not base_cfg.paper_mode:
            logger.info("Initializing shared session...")
            await client.create_session()
            
        # Filter and normalize weights
        final_active = {}
        if isinstance(active_coins, dict):
            # If the user provided a list of strings instead of a dict, transform it
            # (Though load_active_config should return a dict)
            symbols = list(active_coins.keys())
            for s in symbols:
                w = active_coins.get(s, 0)
                if not isinstance(w, (int, float)) or w <= 0:
                    active_coins[s] = 1.0 / len(symbols)
            
            # Re-normalize just in case
            total_w = sum(active_coins.values())
            if total_w > 0:
                final_active = {s: w / total_w for s, w in active_coins.items()}
            else:
                final_active = {s: 1.0 / len(symbols) for s in symbols}
        
        traders = []
        for symbol, weight in final_active.items():
            logger.info(f"Starting instance for {symbol} (Allocation Weight: {weight:.2%})")
            coin_cfg = load_coin_config(symbol, base_cfg)
            
            trader = LiveTrader(coin_cfg, client=client, allocation_weight=weight)
            traders.append(trader.start())
            
        logger.info(f"Successfully launched {len(traders)} trading instances.")
        
        # Run all traders concurrently
        await asyncio.gather(*traders)
        
    except Exception as e:
        logger.error(f"Multi-runner error: {e}", exc_info=True)
        update_state(status="error", log_msg=f"CRITICAL ERROR: {e}")
    finally:
        await client.close()
        logger.info("Multi-runner shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(run_multi_bot())
    except KeyboardInterrupt:
        pass
