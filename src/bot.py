"""
Main trading bot: fetches OHLCV data, evaluates strategy, manages orders.
"""

import time
import uuid
from typing import Optional

import ccxt
import pandas as pd
from loguru import logger

from src.risk_manager import RiskManager
from src.strategy import AccelerationBreakoutStrategy, Signal, TradeSetup


TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400,
}


class AccelerationBreakoutBot:
    def __init__(self, config: dict, exchange_cfg: dict):
        self.config = config
        self.symbol: str = config["trading"]["symbol"]
        self.timeframe: str = config["trading"]["timeframe"]

        self.strategy = AccelerationBreakoutStrategy(config)
        self.risk = RiskManager(config)

        self.exchange = self._init_exchange(exchange_cfg)
        self._running = False

    # ------------------------------------------------------------------
    # Exchange setup
    # ------------------------------------------------------------------

    def _init_exchange(self, cfg: dict) -> ccxt.Exchange:
        exchange_cls = getattr(ccxt, cfg["exchange"])
        params = {
            "apiKey": cfg.get("api_key", ""),
            "secret": cfg.get("api_secret", ""),
            "enableRateLimit": True,
        }
        if cfg.get("testnet"):
            params["options"] = {"defaultType": "future"}
            params["urls"] = {"api": exchange_cls().urls.get("test", exchange_cls().urls["api"])}

        exchange = exchange_cls(params)
        logger.info(f"Exchange initialised: {exchange.id} | symbol={self.symbol}")
        return exchange

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def fetch_ohlcv(self, limit: int = 200) -> pd.DataFrame:
        raw = self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df

    def get_balance(self) -> float:
        balance = self.exchange.fetch_balance()
        return float(balance["USDT"]["free"])

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def place_market_order(self, side: str, quantity: float) -> Optional[dict]:
        try:
            order = self.exchange.create_market_order(self.symbol, side, quantity)
            logger.info(f"Market {side} order filled: qty={quantity} id={order['id']}")
            return order
        except ccxt.BaseError as e:
            logger.error(f"Order failed: {e}")
            return None

    def _open_position(self, setup: TradeSetup, balance: float):
        if not self.risk.can_open_trade():
            logger.info("Max open trades reached, skipping signal")
            return

        qty = self.risk.position_size(balance, setup.entry, setup.stop_loss)
        if qty <= 0:
            logger.warning("Calculated quantity is zero, skipping")
            return

        side = "buy" if setup.signal == Signal.LONG else "sell"
        order = self.place_market_order(side, qty)
        if order:
            trade_id = str(uuid.uuid4())[:8]
            self.risk.register_trade(trade_id, setup, qty, self.symbol)
            logger.info(f"Position opened | {setup.reason}")

    def _manage_open_trades(self, current_price: float, balance: float):
        self.risk.update_trailing_stops(current_price)

        for trade_id, trade in list(self.risk.open_trades.items()):
            exit_reason = self.risk.check_exit(trade, current_price)
            if exit_reason:
                close_side = "sell" if trade.signal == Signal.LONG else "buy"
                order = self.place_market_order(close_side, trade.quantity)
                if order:
                    logger.info(
                        f"Trade [{trade_id}] closed via {exit_reason.upper()} "
                        f"at {current_price:.4f}"
                    )
                    self.risk.close_trade(trade_id)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _seconds_to_next_candle(self) -> float:
        interval = TIMEFRAME_SECONDS.get(self.timeframe, 900)
        now = time.time()
        return interval - (now % interval) + 2   # +2s buffer

    def run(self):
        logger.info("Bot started — Acceleration Breakout Strategy")
        self._running = True

        while self._running:
            try:
                df = self.fetch_ohlcv(limit=250)
                current_price = float(df["close"].iloc[-1])

                self._manage_open_trades(current_price, self.get_balance())

                setup = self.strategy.generate_signal(df)
                if setup:
                    logger.info(f"Signal: {setup.signal.value.upper()} | {setup.reason}")
                    self._open_position(setup, self.get_balance())

                wait = self._seconds_to_next_candle()
                logger.debug(f"Sleeping {wait:.0f}s until next candle")
                time.sleep(wait)

            except ccxt.NetworkError as e:
                logger.warning(f"Network error: {e}, retrying in 30s")
                time.sleep(30)
            except ccxt.ExchangeError as e:
                logger.error(f"Exchange error: {e}, retrying in 60s")
                time.sleep(60)
            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                self._running = False

    def stop(self):
        self._running = False
