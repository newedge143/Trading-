"""
Market regime detector.

Classifies the most recent bar as one of:
  - TRENDING       : strong directional movement (ADX high, EMA200 sloping)
  - RANGING        : sideways chop (ADX low, EMA flat)
  - TRANSITIONAL   : in between — skip trades, regime unclear

Uses two signals:
  1. ADX(14) — Wilder's directional strength index
       > adx_trend (default 25) = strong trend
       < adx_range (default 18) = chop
  2. EMA200 slope normalized by price
       |slope_pct| > slope_threshold (default 0.0008 / 0.08% per bar) = directional

A bar is TRENDING if BOTH ADX is high AND EMA slope is directional.
A bar is RANGING if BOTH ADX is low AND EMA slope is flat.
Otherwise TRANSITIONAL.
"""

from enum import Enum

import pandas as pd

from src.indicators import adx, ema


class Regime(Enum):
    TRENDING     = "trending"
    RANGING      = "ranging"
    TRANSITIONAL = "transitional"


class RegimeDetector:
    def __init__(
        self,
        adx_period: int = 14,
        ema_period: int = 200,
        slope_lookback: int = 20,
        adx_trend: float = 25.0,
        adx_range: float = 18.0,
        slope_threshold: float = 0.0008,
    ):
        self.adx_period      = adx_period
        self.ema_period      = ema_period
        self.slope_lookback  = slope_lookback
        self.adx_trend       = adx_trend
        self.adx_range       = adx_range
        self.slope_threshold = slope_threshold

    def detect(self, df: pd.DataFrame) -> Regime:
        """Classify the regime of the most recent fully closed bar."""
        h, l, c = df["high"], df["low"], df["close"]

        adx_val   = adx(h, l, c, self.adx_period).iloc[-2]
        ema_slow  = ema(c, self.ema_period)
        slope_pct = (ema_slow.iloc[-2] - ema_slow.iloc[-2 - self.slope_lookback]) \
                    / ema_slow.iloc[-2 - self.slope_lookback]

        if pd.isna(adx_val) or pd.isna(slope_pct):
            return Regime.TRANSITIONAL

        is_trending = adx_val > self.adx_trend and abs(slope_pct) > self.slope_threshold
        is_ranging  = adx_val < self.adx_range and abs(slope_pct) < self.slope_threshold / 2

        if is_trending:
            return Regime.TRENDING
        if is_ranging:
            return Regime.RANGING
        return Regime.TRANSITIONAL

    def diagnostic(self, df: pd.DataFrame) -> dict:
        """Return ADX value, slope %, and detected regime — useful for logging."""
        h, l, c = df["high"], df["low"], df["close"]
        adx_val  = adx(h, l, c, self.adx_period).iloc[-2]
        ema_slow = ema(c, self.ema_period)
        slope_pct = (ema_slow.iloc[-2] - ema_slow.iloc[-2 - self.slope_lookback]) \
                    / ema_slow.iloc[-2 - self.slope_lookback]
        return {
            "adx":       float(adx_val) if pd.notna(adx_val) else None,
            "slope_pct": float(slope_pct) if pd.notna(slope_pct) else None,
            "regime":    self.detect(df).value,
        }
