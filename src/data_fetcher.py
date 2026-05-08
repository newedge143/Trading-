"""
Fetch historical OHLCV data from an exchange and save to CSV.
Used for backtesting.
"""

import time
from pathlib import Path

import ccxt
import pandas as pd
from loguru import logger


def fetch_historical(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    out_dir: str = "data",
) -> pd.DataFrame:
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    since = exchange.parse8601(f"{start_date}T00:00:00Z")
    end_ts = exchange.parse8601(f"{end_date}T00:00:00Z")

    all_ohlcv = []
    logger.info(f"Fetching {symbol} {timeframe} from {start_date} to {end_date}")

    while since < end_ts:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        all_ohlcv.extend(batch)
        since = batch[-1][0] + 1
        time.sleep(exchange.rateLimit / 1000)

    df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[df.index < pd.Timestamp(end_date, tz="UTC")]
    df = df[~df.index.duplicated()]

    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    filename = path / f"{symbol.replace('/', '_')}_{timeframe}.csv"
    df.to_csv(filename)
    logger.info(f"Saved {len(df)} bars to {filename}")
    return df


def load_csv(filepath: str) -> pd.DataFrame:
    df = pd.read_csv(filepath, index_col="timestamp", parse_dates=True)
    return df
