"""
Entry point for the Acceleration Breakout Trading Bot.

Usage:
  python main.py --mode live        # run live trading
  python main.py --mode backtest    # run backtest on historical data
  python main.py --mode download    # download historical data only
"""

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from loguru import logger

load_dotenv()


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run_live(config: dict):
    from src.bot import AccelerationBreakoutBot

    exchange_cfg = {
        "exchange": os.getenv("EXCHANGE", "binance"),
        "api_key": os.getenv("API_KEY", ""),
        "api_secret": os.getenv("API_SECRET", ""),
        "testnet": os.getenv("TESTNET", "true").lower() == "true",
    }

    bot = AccelerationBreakoutBot(config, exchange_cfg)
    bot.run()


def run_backtest(config: dict, data_file: str = None):
    from src.backtest import Backtester
    from src.data_fetcher import fetch_historical, load_csv

    bt_cfg = config["backtest"]

    if data_file and Path(data_file).exists():
        logger.info(f"Loading data from {data_file}")
        df = load_csv(data_file)
    else:
        logger.info("Downloading historical data for backtest...")
        df = fetch_historical(
            exchange_id="binance",
            symbol=config["trading"]["symbol"],
            timeframe=config["trading"]["timeframe"],
            start_date=bt_cfg["start_date"],
            end_date=bt_cfg["end_date"],
        )

    backtester = Backtester(config)
    result = backtester.run(df)

    # Optional: save trade log
    if result.trades:
        import pandas as pd
        trades_df = pd.DataFrame([vars(t) for t in result.trades])
        trades_df.to_csv("data/backtest_trades.csv", index=False)
        logger.info("Trade log saved to data/backtest_trades.csv")

    return result


def run_download(config: dict):
    from src.data_fetcher import fetch_historical

    bt_cfg = config["backtest"]
    fetch_historical(
        exchange_id="binance",
        symbol=config["trading"]["symbol"],
        timeframe=config["trading"]["timeframe"],
        start_date=bt_cfg["start_date"],
        end_date=bt_cfg["end_date"],
    )


def main():
    parser = argparse.ArgumentParser(description="Acceleration Breakout Trading Bot")
    parser.add_argument(
        "--mode",
        choices=["live", "backtest", "download"],
        default="backtest",
        help="Execution mode (default: backtest)",
    )
    parser.add_argument("--config", default="config/config.yaml", help="Path to config file")
    parser.add_argument("--data", default=None, help="Path to existing CSV for backtest")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
    logger.add("data/bot.log", rotation="10 MB", level="DEBUG")

    config = load_config(args.config)

    if args.mode == "live":
        run_live(config)
    elif args.mode == "backtest":
        run_backtest(config, args.data)
    elif args.mode == "download":
        run_download(config)


if __name__ == "__main__":
    main()
