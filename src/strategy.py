"""
Acceleration Breakout Strategy
--------------------------------
Logic:
  1. Detect tight consolidation using BB-squeeze or ATR-based range compression.
  2. Confirm momentum acceleration: fast ROC rising faster than slow ROC.
  3. Enter LONG when price closes above consolidation high with positive acceleration.
     Enter SHORT when price closes below consolidation low with negative acceleration.
  4. Stop loss just beyond the opposite side of the consolidation range.
  5. Take profit at reward_risk_ratio × risk distance.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from src.indicators import (
    atr,
    bb_squeeze,
    consolidation_range,
    momentum_histogram,
    volume_sma,
)


class Signal(Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


@dataclass
class TradeSetup:
    signal: Signal
    entry: float
    stop_loss: float
    take_profit: float
    reason: str


class AccelerationBreakoutStrategy:
    def __init__(self, config: dict):
        s = config["strategy"]
        self.consol_period = s["consolidation_period"]
        self.consol_atr_mult = s["consolidation_atr_mult"]
        self.bb_period = s["squeeze_bb_period"]
        self.bb_std = s["squeeze_bb_std"]
        self.kc_mult = s["squeeze_kc_mult"]
        self.roc_fast = s["roc_fast"]
        self.roc_slow = s["roc_slow"]
        self.accel_threshold = s["accel_threshold"]
        self.breakout_bars = s["breakout_bars"]
        self.volume_mult = s["volume_mult"]

        r = config["risk"]
        self.rr_ratio = r["reward_risk_ratio"]

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add all indicator columns to the OHLCV dataframe."""
        df = df.copy()
        h, l, c, v = df["high"], df["low"], df["close"], df["volume"]

        # Consolidation
        squeeze = bb_squeeze(h, l, c, self.bb_period, self.bb_std, self.kc_mult)
        in_consol, roll_high, roll_low = consolidation_range(
            h, l, c, self.consol_period, self.consol_atr_mult
        )
        df["squeeze"] = squeeze
        df["in_consol"] = in_consol | squeeze   # either method flags consolidation
        df["consol_high"] = roll_high
        df["consol_low"] = roll_low

        # Momentum acceleration histogram
        df["accel"] = momentum_histogram(c, self.roc_fast, self.roc_slow)

        # ATR for stop sizing
        df["atr"] = atr(h, l, c, 14)

        # Volume filter
        df["vol_sma"] = volume_sma(v, 20)

        return df

    def _breakout_confirmed(self, df: pd.DataFrame, idx: int, direction: Signal) -> bool:
        """Check that the last `breakout_bars` closed outside the consolidation range."""
        if idx < self.breakout_bars:
            return False
        rows = df.iloc[idx - self.breakout_bars + 1 : idx + 1]
        if direction == Signal.LONG:
            return all(rows["close"] > rows["consol_high"])
        return all(rows["close"] < rows["consol_low"])

    def _volume_confirmed(self, df: pd.DataFrame, idx: int) -> bool:
        row = df.iloc[idx]
        return row["volume"] > self.volume_mult * row["vol_sma"]

    def generate_signal(self, df: pd.DataFrame) -> Optional[TradeSetup]:
        """
        Evaluate the latest completed bar and return a TradeSetup or None.
        Call this on each new closed candle.
        """
        df = self.compute_indicators(df)
        if len(df) < max(self.consol_period, self.bb_period, self.roc_slow) + 5:
            return None

        # Use second-to-last bar (last fully closed bar)
        idx = len(df) - 2
        row = df.iloc[idx]

        # Must have been in consolidation recently
        lookback = min(self.consol_period, idx)
        was_in_consol = df["in_consol"].iloc[idx - lookback : idx].any()
        if not was_in_consol:
            return None

        accel = row["accel"]
        consol_high = row["consol_high"]
        consol_low = row["consol_low"]
        close = row["close"]
        current_atr = row["atr"]

        # ---- LONG setup ----
        if (
            close > consol_high
            and accel > self.accel_threshold
            and self._breakout_confirmed(df, idx, Signal.LONG)
            and self._volume_confirmed(df, idx)
        ):
            entry = close
            stop_loss = consol_low - 0.1 * current_atr   # just below consolidation
            risk = entry - stop_loss
            if risk <= 0:
                return None
            take_profit = entry + self.rr_ratio * risk
            return TradeSetup(
                signal=Signal.LONG,
                entry=entry,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                reason=f"Breakout above {consol_high:.2f} with accel={accel:.4f}",
            )

        # ---- SHORT setup ----
        if (
            close < consol_low
            and accel < -self.accel_threshold
            and self._breakout_confirmed(df, idx, Signal.SHORT)
            and self._volume_confirmed(df, idx)
        ):
            entry = close
            stop_loss = consol_high + 0.1 * current_atr  # just above consolidation
            risk = stop_loss - entry
            if risk <= 0:
                return None
            take_profit = entry - self.rr_ratio * risk
            return TradeSetup(
                signal=Signal.SHORT,
                entry=entry,
                stop_loss=round(stop_loss, 8),
                take_profit=round(take_profit, 8),
                reason=f"Breakdown below {consol_low:.2f} with accel={accel:.4f}",
            )

        return None
