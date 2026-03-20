"""Binance Futures data client — OHLCV klines + real Open Interest."""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Binance OI hist endpoint supports only these periods
_VALID_OI_PERIODS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}


class BinanceDataClient:
    """Fetches OHLCV klines and Open Interest history from Binance Futures."""

    BASE_URL = "https://fapi.binance.com"

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "mm-bot/1.0"})

    def fetch_klines_with_oi(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV klines and merge with real Open Interest history.

        Args:
            symbol:   Binance Futures symbol, e.g. 'SOLUSDT'
            interval: Candle interval, e.g. '1m', '5m'
            limit:    Number of candles (max 1500)

        Returns:
            DataFrame with columns: timestamp, open, high, low, close,
            volume, oi — indexed by datetime UTC.
        """
        df = self._fetch_klines(symbol, interval, limit)
        if df.empty:
            return df

        # Fetch real OI only for supported intervals
        if interval in _VALID_OI_PERIODS:
            oi_df = self._fetch_oi_hist(symbol, interval, limit)
            if not oi_df.empty:
                df = df.merge(oi_df[["timestamp", "oi"]], on="timestamp", how="left")
                df["oi"] = pd.to_numeric(df["oi"], errors="coerce")
                df["oi"] = df["oi"].ffill().fillna(0)
            else:
                df["oi"] = 0
        else:
            df["oi"] = 0

        return df

    # ───────────────────────── private helpers ──────────────────────────

    def _fetch_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        """Fetch raw OHLCV klines from Binance Futures /fapi/v1/klines."""
        try:
            resp = self._session.get(
                f"{self.BASE_URL}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"[Binance] Klines fetch failed for {symbol} ({interval}): {e}")
            return pd.DataFrame()

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(
            data,
            columns=[
                "timestamp", "open", "high", "low", "close", "volume",
                "close_time", "quote_asset_volume", "number_of_trades",
                "taker_buy_base", "taker_buy_quote", "ignore",
            ],
        )

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["timestamp"] = pd.to_numeric(df["timestamp"])
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.index.name = "datetime"

        return df[["timestamp", "open", "high", "low", "close", "volume"]]

    def _fetch_oi_hist(self, symbol: str, period: str, limit: int) -> pd.DataFrame:
        """
        Fetch Open Interest history from Binance Futures.
        Endpoint: GET /futures/data/openInterestHist
        """
        try:
            resp = self._session.get(
                f"{self.BASE_URL}/futures/data/openInterestHist",
                params={"symbol": symbol, "period": period, "limit": limit},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[Binance] OI hist fetch failed for {symbol}: {e}")
            return pd.DataFrame()

        if not data or not isinstance(data, list):
            return pd.DataFrame()

        oi_df = pd.DataFrame(data)

        # Field names returned by Binance OI endpoint:
        # {"symbol", "sumOpenInterest", "sumOpenInterestValue", "timestamp"}
        if "timestamp" not in oi_df.columns or "sumOpenInterest" not in oi_df.columns:
            logger.warning(f"[Binance] Unexpected OI response format for {symbol}: {oi_df.columns.tolist()}")
            return pd.DataFrame()

        oi_df["oi"] = pd.to_numeric(oi_df["sumOpenInterest"], errors="coerce")
        oi_df["timestamp"] = pd.to_numeric(oi_df["timestamp"])

        return oi_df[["timestamp", "oi"]]


def o1_to_binance(symbol: str) -> str:
    """Convert 01 Exchange symbol to Binance Futures symbol, e.g. SOLUSD → SOLUSDT."""
    if symbol.endswith("-PERP"):
        return symbol.replace("-PERP", "USDT")
    elif symbol.endswith("USD"):
        return symbol.replace("USD", "USDT")
    return symbol
