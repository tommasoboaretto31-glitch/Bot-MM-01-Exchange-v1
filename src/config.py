"""Configuration loader ? reads TOML config + env vars."""

from __future__ import annotations

import os
import sys
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

def get_bundle_dir() -> Path:
    """Get the internal directory where assets are bundled (inside EXE)."""
    if getattr(sys, 'frozen', False):
        # PyInstaller: sys._MEIPASS, Nuitka: sys.prefix or relative to __file__
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent.parent

def get_exec_dir() -> Path:
    """Get the external directory where the EXE is actually residing (persistence)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent

# External persistence (next to EXE)
ROOT_DIR = get_exec_dir()
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"
LOG_DIR = ROOT_DIR / "logs"

# Ensure directories exist BEFORE anything uses them
try:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    # If this fails, the app will crash later with WinError 3, 
    # so we print it now for the launcher logs
    print(f"DEBUG: Failed to create directories at {ROOT_DIR}: {e}")

# Internal resources (bundled inside EXE)
BUNDLE_DIR = get_bundle_dir()
ASSETS_DIR = BUNDLE_DIR / "assets"
STATIC_DIR = BUNDLE_DIR / "src" / "dashboard" / "static"


@dataclass
class MarketMakerConfig:
# ... (same as before) ...
    spread_bps: float = 30.0
    order_size_pct: float = 60.0
    max_inventory_pct: float = 80.0
    stale_candles: int = 20
    use_atr_spread: bool = True
    atr_spread_mult: float = 0.4
    min_spread_bps: float = 25.0
    fixed_tp_bps: float = 0.0
    tp_atr_mult: float = 1.5
    inventory_skew_factor: float = 0.5
    stop_loss_bps: float = 15.0
    volatility_pause_mult: float = 3.0

@dataclass
class GridConfig:
    levels: int = 5
    spacing_atr_mult: float = 0.5
    rebalance_threshold: float = 0.3
    max_open_orders: int = 10


@dataclass
class IndicatorConfig:
    rsi_period: int = 14
    rsi_overbought: int = 70
    rsi_oversold: int = 30
    adx_period: int = 14
    adx_trend_threshold: int = 25
    atr_period: int = 14
    vwap_session_hours: int = 24
    vwap_max_distance_pct: float = 2.0
    momentum_period: int = 10


@dataclass
class HeatmapConfig:
    update_interval: str = "candle"
    depth_levels: int = 20
    rolling_window_minutes: int = 30
    ema_smoothing: int = 5
    min_liquidity_ratio: float = 0.1


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0
    stop_atr_mult_range: float = 1.5
    stop_atr_mult_trend: float = 2.5
    max_daily_drawdown_pct: float = 5.0
    max_position_pct: float = 20.0
    min_spread_bps: int = 5
    max_spread_bps: int = 100


@dataclass
class FeeConfig:
    maker_fee_pct: float = 0.02
    taker_fee_pct: float = 0.05


@dataclass
class BacktestConfig:
    slippage_bps: float = 3.0
    initial_capital: float = 50.0


@dataclass
class MarketInfo:
    """Market metadata from 01 Exchange /info endpoint."""
    market_id: int
    symbol: str
    price_decimals: int
    size_decimals: int
    imf: float  # Initial margin fraction
    mmf: float  # Maintenance margin fraction

    @property
    def max_leverage(self) -> float:
        return 1.0 / self.imf if self.imf > 0 else 1.0

    @property
    def bybit_symbol(self) -> str:
        """Map 01 symbol to Bybit perpetual symbol."""
        base = self.symbol.replace("USD", "")
        return f"{base}USDT"


@dataclass
class Config:
    """Master configuration."""
    capital: float = 50.0
    timeframe: str = "5m"
    paper_mode: bool = True
    symbols: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    market_maker: MarketMakerConfig = field(default_factory=MarketMakerConfig)
    grid: GridConfig = field(default_factory=GridConfig)
    indicators: IndicatorConfig = field(default_factory=IndicatorConfig)
    heatmap: HeatmapConfig = field(default_factory=HeatmapConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    # API
    api_url: str = "https://zo-mainnet.n1.xyz"
    ws_url: str = "wss://zo-mainnet.n1.xyz/ws/"
    keypair_path: str = "./id.json"

    @property
    def active_symbols(self) -> list[str]:
        return [s for s in self.symbols if s not in self.excluded]


def _flat_to_dataclass(data: dict[str, Any], cls: Any) -> Any:
    """Map a flat dict to a dataclass, ignoring unknown keys."""
    import dataclasses
    valid = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML file + environment overrides, with bundled fallback."""
    import shutil
    import tomllib
    
    load_dotenv()
    
    ext_path = path or CONFIG_DIR / "default.toml"
    bundle_path = BUNDLE_DIR / "config" / "default.toml"

    # Strategy: 
    # 1. If external exists, use it.
    # 2. If not, try to copy from bundle to external.
    # 3. If copy fails, use bundle directly.
    # 4. If nothing works, use default Config object.

    final_path = ext_path
    if not ext_path.exists():
        if bundle_path.exists():
            try:
                shutil.copy(bundle_path, ext_path)
                logger.info("Created default config at %s", ext_path)
            except Exception as e:
                logger.warning("Failed to copy bundled config: %s. Using internal assets.", e)
                final_path = bundle_path
        else:
            logger.error("No configuration found anywhere!")
            return Config()

    try:
        with open(final_path, "rb") as f:
            raw = tomllib.load(f)
    except Exception as e:
        logger.error("Failed to parse config %s: %s", final_path, e)
        return Config()

    general = raw.get("general", {})
    markets = raw.get("markets", {})

    cfg = Config(
        capital=general.get("capital", 50.0),
        timeframe=general.get("timeframe", "5m"),
        paper_mode=general.get("paper_mode", True),
        symbols=markets.get("symbols", []),
        excluded=markets.get("excluded", []),
        market_maker=_flat_to_dataclass(raw.get("market_maker", {}), MarketMakerConfig),
        grid=_flat_to_dataclass(raw.get("grid", {}), GridConfig),
        indicators=_flat_to_dataclass(raw.get("indicators", {}), IndicatorConfig),
        heatmap=_flat_to_dataclass(raw.get("heatmap", {}), HeatmapConfig),
        risk=_flat_to_dataclass(raw.get("risk", {}), RiskConfig),
        fees=_flat_to_dataclass(raw.get("fees", {}), FeeConfig),
        backtest=_flat_to_dataclass(raw.get("backtest", {}), BacktestConfig),
    )

    # Resolve keypair path relative to ROOT_DIR if relative
    kp = os.getenv("KEYPAIR_PATH", cfg.keypair_path)
    if not Path(kp).is_absolute():
        cfg.keypair_path = str(ROOT_DIR / kp)
    else:
        cfg.keypair_path = kp

    cfg.api_url = os.getenv("O1_API_URL", cfg.api_url)
    cfg.ws_url = os.getenv("O1_WS_URL", cfg.ws_url)

    return cfg


def load_active_config() -> dict[str, float]:
    """Load active coins and their weights from config/active.toml."""
    path = CONFIG_DIR / "active.toml"
    if not path.exists():
        return {}
    
    try:
        import tomllib
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return data.get("active", {})
    except Exception as e:
        logger.error("Failed to load active.toml: %s", e)
        return {}


def load_coin_config(symbol: str, base_cfg: Config | None = None) -> Config:
    """Load specific config for a coin, overriding the base config."""
    if base_cfg is None:
        base_cfg = load_config()
    
    coin_path = CONFIG_DIR / "coins" / f"{symbol}.toml"
    if not coin_path.exists():
        # Just update the symbol and return
        base_cfg.symbols = [symbol]
        return base_cfg
    
    try:
        import tomllib
        with open(coin_path, "rb") as f:
            overrides = tomllib.load(f)
        
        # Merge overrides into a copy of base_cfg
        import copy
        cfg = copy.deepcopy(base_cfg)
        cfg.symbols = [symbol]
        
        import dataclasses as _dc
        if "market_maker" in overrides:
            merged = _dc.asdict(cfg.market_maker)
            merged.update(overrides["market_maker"])
            cfg.market_maker = _flat_to_dataclass(merged, MarketMakerConfig)
        if "risk" in overrides:
            merged = _dc.asdict(cfg.risk)
            merged.update(overrides["risk"])
            cfg.risk = _flat_to_dataclass(merged, RiskConfig)
        if "indicators" in overrides:
            merged = _dc.asdict(cfg.indicators)
            merged.update(overrides["indicators"])
            cfg.indicators = _flat_to_dataclass(merged, IndicatorConfig)
            
        return cfg
    except Exception as e:
        logger.error("Failed to load config for %s: %s", symbol, e)
        base_cfg.symbols = [symbol]
        return base_cfg
