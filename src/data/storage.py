import json
import os
import pandas as pd

DATA_DIR = "./data"

def save_candles(symbol: str, timeframe: str, df: pd.DataFrame):
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = f"{DATA_DIR}/{symbol}_{timeframe}.csv"
    df.to_csv(filename, index=False)

def load_candles(symbol: str, timeframe: str) -> pd.DataFrame:
    filename = f"{DATA_DIR}/{symbol}_{timeframe}.csv"
    if os.path.exists(filename):
        return pd.read_csv(filename)
    return pd.DataFrame()

def list_cached() -> list:
    if not os.path.exists(DATA_DIR):
        return []
    return os.listdir(DATA_DIR)
