from dataclasses import dataclass
import pandas as pd
from datetime import datetime

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

class CandleAggregator:
    def __init__(self, timeframe: str):
        self.timeframe = timeframe
        self.candles = []
        self.current_candle = None
        self.tf_ms = self._tf_to_ms(timeframe)

    def _tf_to_ms(self, tf: str) -> int:
        mult = int(tf[:-1])
        unit = tf[-1]
        if unit == 'm':
            return mult * 60 * 1000
        elif unit == 'h':
            return mult * 60 * 60 * 1000
        elif unit == 'd':
            return mult * 24 * 60 * 60 * 1000
        return 60000 # default 1m

    def preload(self, candles: list[Candle]):
        self.candles.extend(candles)

    def update(self, price: float, size: float, timestamp: int) -> bool:
        if self.current_candle is None:
            candle_ts = timestamp - (timestamp % self.tf_ms)
            self.current_candle = Candle(candle_ts, price, price, price, price, size)
            return False

        if timestamp >= self.current_candle.timestamp + self.tf_ms:
            self.candles.append(self.current_candle)
            candle_ts = timestamp - (timestamp % self.tf_ms)
            self.current_candle = Candle(candle_ts, price, price, price, price, size)
            return True

        self.current_candle.high = max(self.current_candle.high, price)
        self.current_candle.low = min(self.current_candle.low, price)
        self.current_candle.close = price
        self.current_candle.volume += size
        return False

    def to_df(self) -> pd.DataFrame:
        all_candles = self.candles.copy()
        if self.current_candle is not None:
            all_candles.append(self.current_candle)

        if not all_candles:
            return pd.DataFrame()

        df = pd.DataFrame([c.__dict__ for c in all_candles])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
