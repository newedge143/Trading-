"""
Risk management: position sizing and trade tracking.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger

from src.strategy import Signal, TradeSetup


@dataclass
class OpenTrade:
    id: str
    signal: Signal
    entry: float
    stop_loss: float
    take_profit: float
    quantity: float
    symbol: str


class RiskManager:
    def __init__(self, config: dict):
        r = config["risk"]
        self.risk_per_trade: float = r["risk_per_trade"]
        self.rr_ratio: float = r["reward_risk_ratio"]
        self.max_open_trades: int = r["max_open_trades"]
        self.trailing_stop: bool = r["trailing_stop"]
        self.trailing_pct: float = r["trailing_stop_pct"]

        self.open_trades: Dict[str, OpenTrade] = {}

    def can_open_trade(self) -> bool:
        return len(self.open_trades) < self.max_open_trades

    def position_size(self, balance: float, entry: float, stop_loss: float) -> float:
        """Kelly-lite fixed-risk sizing: risk exactly `risk_per_trade` of balance."""
        risk_amount = balance * self.risk_per_trade
        per_unit_risk = abs(entry - stop_loss)
        if per_unit_risk == 0:
            return 0.0
        qty = risk_amount / per_unit_risk
        return round(qty, 6)

    def register_trade(self, trade_id: str, setup: TradeSetup, qty: float, symbol: str):
        self.open_trades[trade_id] = OpenTrade(
            id=trade_id,
            signal=setup.signal,
            entry=setup.entry,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            quantity=qty,
            symbol=symbol,
        )
        logger.info(
            f"Trade registered [{trade_id}] {setup.signal.value.upper()} "
            f"qty={qty} entry={setup.entry} sl={setup.stop_loss} tp={setup.take_profit}"
        )

    def close_trade(self, trade_id: str):
        if trade_id in self.open_trades:
            del self.open_trades[trade_id]
            logger.info(f"Trade closed [{trade_id}]")

    def update_trailing_stops(self, current_price: float):
        """Ratchet stop loss upward (LONG) or downward (SHORT) if trailing is enabled."""
        if not self.trailing_stop:
            return
        for trade in self.open_trades.values():
            if trade.signal == Signal.LONG:
                new_sl = current_price * (1 - self.trailing_pct)
                if new_sl > trade.stop_loss:
                    logger.debug(f"Trailing SL updated {trade.stop_loss:.4f} → {new_sl:.4f}")
                    trade.stop_loss = new_sl
            else:
                new_sl = current_price * (1 + self.trailing_pct)
                if new_sl < trade.stop_loss:
                    logger.debug(f"Trailing SL updated {trade.stop_loss:.4f} → {new_sl:.4f}")
                    trade.stop_loss = new_sl

    def check_exit(self, trade: OpenTrade, current_price: float) -> Optional[str]:
        """Return 'sl', 'tp', or None."""
        if trade.signal == Signal.LONG:
            if current_price <= trade.stop_loss:
                return "sl"
            if current_price >= trade.take_profit:
                return "tp"
        else:
            if current_price >= trade.stop_loss:
                return "sl"
            if current_price <= trade.take_profit:
                return "tp"
        return None
