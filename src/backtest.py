"""
Vectorised backtesting engine for the Acceleration Breakout strategy.
"""

from dataclasses import dataclass, field
from typing import List

import pandas as pd
import numpy as np
from loguru import logger

from src.strategy import AccelerationBreakoutStrategy, Signal


@dataclass
class BacktestTrade:
    signal: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str      # "sl" | "tp" | "end"
    pnl_pct: float
    pnl_abs: float


@dataclass
class BacktestResult:
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl_abs > 0)
        return wins / len(self.trades)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_abs for t in self.trades)

    @property
    def max_drawdown(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        roll_max = self.equity_curve.cummax()
        dd = (self.equity_curve - roll_max) / roll_max
        return float(dd.min())

    @property
    def sharpe(self) -> float:
        if self.equity_curve.empty or len(self.equity_curve) < 2:
            return 0.0
        returns = self.equity_curve.pct_change().dropna()
        if returns.std() == 0:
            return 0.0
        return float(returns.mean() / returns.std() * np.sqrt(252))

    def summary(self) -> str:
        return (
            f"\n{'='*50}\n"
            f"  Backtest Results\n"
            f"{'='*50}\n"
            f"  Trades        : {self.num_trades}\n"
            f"  Win Rate      : {self.win_rate:.1%}\n"
            f"  Total PnL     : ${self.total_pnl:,.2f}\n"
            f"  Max Drawdown  : {self.max_drawdown:.2%}\n"
            f"  Sharpe Ratio  : {self.sharpe:.2f}\n"
            f"  Final Equity  : ${self.equity_curve.iloc[-1]:,.2f}\n"
            f"{'='*50}"
        )


class Backtester:
    def __init__(self, config: dict):
        self.config = config
        self.strategy = AccelerationBreakoutStrategy(config)
        self.commission = config["backtest"]["commission"]
        self.initial_capital = config["backtest"]["initial_capital"]
        self.risk_per_trade = config["risk"]["risk_per_trade"]
        self.rr_ratio = config["risk"]["reward_risk_ratio"]

    def run(self, df: pd.DataFrame) -> BacktestResult:
        df = self.strategy.compute_indicators(df)
        capital = self.initial_capital
        equity = [capital]
        timestamps = [df.index[0]]
        trades: List[BacktestTrade] = []

        in_trade = False
        trade_signal = None
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        entry_time = None
        qty = 0.0

        min_bars = max(
            self.config["strategy"]["consolidation_period"],
            self.config["strategy"]["squeeze_bb_period"],
            self.config["strategy"]["roc_slow"],
        ) + 10

        for i in range(min_bars, len(df) - 1):
            row = df.iloc[i]
            next_row = df.iloc[i + 1]
            current_price = row["close"]
            timestamps.append(row.name)

            # --- Manage open trade ---
            if in_trade:
                # Check SL/TP hit on current bar using high/low
                hit_sl = hit_tp = False
                if trade_signal == Signal.LONG:
                    hit_sl = next_row["low"] <= stop_loss
                    hit_tp = next_row["high"] >= take_profit
                else:
                    hit_sl = next_row["high"] >= stop_loss
                    hit_tp = next_row["low"] <= take_profit

                exit_price = None
                exit_reason = None
                if hit_tp and hit_sl:
                    # Both on same bar: assume TP first if candle favourable, else SL
                    exit_price = take_profit if trade_signal == Signal.LONG else stop_loss
                    exit_reason = "tp"
                elif hit_tp:
                    exit_price = take_profit
                    exit_reason = "tp"
                elif hit_sl:
                    exit_price = stop_loss
                    exit_reason = "sl"

                if exit_price is not None:
                    if trade_signal == Signal.LONG:
                        pnl_pct = (exit_price - entry_price) / entry_price
                    else:
                        pnl_pct = (entry_price - exit_price) / entry_price
                    pnl_pct -= self.commission * 2
                    pnl_abs = pnl_pct * qty * entry_price
                    capital += pnl_abs
                    trades.append(
                        BacktestTrade(
                            signal=trade_signal.value,
                            entry_time=entry_time,
                            entry_price=entry_price,
                            exit_time=next_row.name,
                            exit_price=exit_price,
                            exit_reason=exit_reason,
                            pnl_pct=pnl_pct,
                            pnl_abs=pnl_abs,
                        )
                    )
                    in_trade = False

            # --- Look for new signal (only if flat) ---
            if not in_trade:
                window = df.iloc[: i + 1]
                setup = self.strategy.generate_signal(window)
                if setup:
                    entry_price = setup.entry
                    stop_loss = setup.stop_loss
                    take_profit = setup.take_profit
                    trade_signal = setup.signal
                    entry_time = row.name
                    risk_amount = capital * self.risk_per_trade
                    per_unit_risk = abs(entry_price - stop_loss)
                    qty = risk_amount / per_unit_risk if per_unit_risk > 0 else 0
                    in_trade = qty > 0

            equity.append(capital)

        # Close any open trade at end of data
        if in_trade:
            exit_price = df.iloc[-1]["close"]
            if trade_signal == Signal.LONG:
                pnl_pct = (exit_price - entry_price) / entry_price - self.commission * 2
            else:
                pnl_pct = (entry_price - exit_price) / entry_price - self.commission * 2
            pnl_abs = pnl_pct * qty * entry_price
            capital += pnl_abs
            trades.append(
                BacktestTrade(
                    signal=trade_signal.value,
                    entry_time=entry_time,
                    entry_price=entry_price,
                    exit_time=df.index[-1],
                    exit_price=exit_price,
                    exit_reason="end",
                    pnl_pct=pnl_pct,
                    pnl_abs=pnl_abs,
                )
            )
            equity.append(capital)
            timestamps.append(df.index[-1])

        equity_series = pd.Series(equity, index=pd.Index(timestamps[: len(equity)]))
        result = BacktestResult(trades=trades, equity_curve=equity_series)
        logger.info(result.summary())
        return result
