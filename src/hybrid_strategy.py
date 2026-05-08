"""
Hybrid Regime-Aware Strategy
=============================
Routes signal generation by detected market regime:

  TRENDING     → AccelerationBreakoutStrategy (EMA200 + RSI trend-pullback)
  RANGING      → ScalpStrategy                (Stoch + ADX + EMA mean-reversion)
  TRANSITIONAL → no trade

This solves the central problem we kept hitting: trend strategies bleed in chop,
mean-reversion bleeds in trends. Pick the right tool for the current condition.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from src.regime import Regime, RegimeDetector
from src.scalp_strategy import ScalpStrategy
from src.strategy import AccelerationBreakoutStrategy
from src.strategy import Signal as StratSignal


class Signal(Enum):
    LONG  = "long"
    SHORT = "short"
    NONE  = "none"


@dataclass
class TradeSetup:
    signal: Signal
    entry: float
    stop_loss: float
    take_profit: float
    reason: str


def _convert(setup) -> Optional[TradeSetup]:
    if setup is None:
        return None
    sig = Signal.LONG if setup.signal.value == "long" else \
          Signal.SHORT if setup.signal.value == "short" else Signal.NONE
    if sig == Signal.NONE:
        return None
    return TradeSetup(
        signal      = sig,
        entry       = setup.entry,
        stop_loss   = setup.stop_loss,
        take_profit = setup.take_profit,
        reason      = setup.reason,
    )


class HybridStrategy:
    def __init__(self, config: dict):
        self.regime    = RegimeDetector(**config.get("regime", {}))
        self.trend     = AccelerationBreakoutStrategy(config["trend"])
        self.range     = ScalpStrategy(config["range"])
        self.last_regime: Optional[Regime] = None

    def generate_signal(self, df: pd.DataFrame) -> Optional[TradeSetup]:
        regime = self.regime.detect(df)
        self.last_regime = regime

        if regime == Regime.TRENDING:
            setup = self.trend.generate_signal(df)
            converted = _convert(setup)
            if converted:
                converted.reason = f"[TRENDING] {converted.reason}"
            return converted

        if regime == Regime.RANGING:
            setup = self.range.generate_signal(df)
            converted = _convert(setup)
            if converted:
                converted.reason = f"[RANGING] {converted.reason}"
            return converted

        # TRANSITIONAL — no trade
        return None
