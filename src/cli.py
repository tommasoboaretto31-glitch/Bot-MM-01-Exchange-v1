"""CLI entry point for NuovoBot."""

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config, LOG_DIR, load_active_config, load_coin_config
from src.live.trader import LiveTrader
from src.api.client import O1Client
from src.dashboard.app import run_dashboard
import src.dashboard.app as app
import os

# Setup logging
log_file = LOG_DIR / "paper_trading.log"
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
# Clear existing handlers to avoid conflicts with launcher
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)
root_logger.addHandler(logging.StreamHandler())
root_logger.addHandler(file_handler)

logger = logging.getLogger("NuovoBot")

async def run_bot_only():
    """Runs the LiveTrader(s)."""
    cfg = load_config()
    # Calculate weights
    active_coins = load_active_config() or {s: 1.0 for s in cfg.active_symbols}
    total_symbols = len(active_coins)
    if total_symbols == 0:
        return
        
    # Standardize weights
    final_active = {}
    for symbol in active_coins:
        w = active_coins[symbol] if isinstance(active_coins, dict) else 1.0
        if not isinstance(w, (int, float)) or w <= 0:
            w = 1.0 / total_symbols
        final_active[symbol] = w
        
    # Re-normalize
    total_w = sum(final_active.values())
    final_active = {s: w / total_w for s, w in final_active.items()}

    client = O1Client(cfg.api_url, keypair_path=cfg.keypair_path)
    trader_tasks = []
    
    for symbol, weight in final_active.items():
        coin_cfg = load_coin_config(symbol, cfg)
        trader = LiveTrader(coin_cfg, client=client, allocation_weight=weight)
        trader_tasks.append(trader.start())
    
    await asyncio.gather(*trader_tasks)

async def run_bot_with_dashboard():
    """Runs both bot and dashboard, dashboard first for responsiveness."""
    cfg = load_config()
    
    # Ensure port is set for the dashboard
    if "DASHBOARD_PORT" not in os.environ:
        os.environ["DASHBOARD_PORT"] = "8000"
        
    # Reset dashboard state so GUI shows correct initial capital
    app.reset_dashboard(cfg.capital)
    
    # CRITICAL: Start Dashboard FIRST
    dashboard_task = asyncio.create_task(run_dashboard(cfg))
    
    # Wait a tiny bit to let uvicorn bind the port
    await asyncio.sleep(2)

    # Calculate weights
    active_coins = load_active_config() or {s: 1.0 for s in cfg.active_symbols}
    total_symbols = len(active_coins)
    if total_symbols == 0:
        logger.error("No active symbols configured!")
        return

    # Standardize weights
    final_active = {}
    for symbol in active_coins:
        w = active_coins[symbol] if isinstance(active_coins, dict) else 1.0
        if not isinstance(w, (int, float)) or w <= 0:
            w = 1.0 / total_symbols
        final_active[symbol] = w
    
    # Re-normalize
    total_w = sum(final_active.values())
    final_active = {s: w / total_w for s, w in final_active.items()}

    client = O1Client(cfg.api_url, keypair_path=cfg.keypair_path)
    trader_tasks = []
    for symbol, weight in final_active.items():
        coin_cfg = load_coin_config(symbol, cfg)
        trader = LiveTrader(coin_cfg, client=client, allocation_weight=weight)
        trader_tasks.append(trader.start())
        
    try:
        # Run all concurrently
        await asyncio.gather(
            *trader_tasks,
            dashboard_task
        )
    except Exception as e:
        logger.error(f"Bot engine crashed: {e}", exc_info=True)

def main():
    """Main CLI entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "--dashboard":
        asyncio.run(run_bot_with_dashboard())
    else:
        asyncio.run(run_bot_only())

if __name__ == "__main__":
    main()
