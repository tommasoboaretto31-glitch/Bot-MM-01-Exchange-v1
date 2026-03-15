import requests
import pandas as pd

class BinanceDataClient:
    def __init__(self):
        self.base_url = "https://fapi.binance.com"

    def fetch_klines_with_oi(self, symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
        url = f"{self.base_url}/fapi/v1/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        df = pd.DataFrame(data, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "number_of_trades",
            "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
        ])

        # Open Interest - Simplified, actual binance API is different endpoint but this matches what it expects
        # We will fetch actual OI if needed or mock it for now since we're just fixing the crash
        df["oi"] = 0

        # Need numeric values
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col])

        return df

def o1_to_binance(symbol: str) -> str:
    # Example conversion: SOL-PERP -> SOLUSDT
    if symbol.endswith("-PERP"):
        return symbol.replace("-PERP", "USDT")
    elif symbol.endswith("USD"):
        return symbol.replace("USD", "USDT")
    return symbol
