"""
Bollinger Band Reversion with Regime Filter
============================================
Based on the recommended approach from cross-source research:
  - freqtrade BinHV45-family strategies (PF 1.4-1.7 on 5m alts)
  - Cont/Kukanov mean-reversion academic basis
  - QuantVPS / Reddit r/algotrading community-verified

ENTRY (LONG) — all must be true:
  1. close ≤ lower BB(20, 2.0)              — price is statistically extended low
  2. RSI(14) ≤ 28                            — momentum confirms oversold
  3. ADX(14) < 25                            — REGIME GATE: only in chop, never trends
  4. volume ≥ 1.0× SMA(volume, 20)           — not a dead market

ENTRY (SHORT) — mirror:
  1. close ≥ upper BB(20, 2.0)
  2. RSI(14) ≥ 72
  3. ADX(14) < 25
  4. volume ≥ 1.0× SMA(volume, 20)

EXIT:
  - TP = middle BB (the 20-SMA) — that's the reversion target
  - SL = 1.2% from entry (hard stop, beyond BB)

This is a TRUE mean-reversion strategy (price extreme → snap back to mean), NOT a
breakout strategy. Only fires in ranging markets, never fights a trend.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from src.indicators import adx, bollinger_bands, rsi, volume_sma


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


class BBReversionStrategy:
    def __init__(self, config: dict):
        s = config.get("strategy", {})
        self.bb_period:    int   = s.get("bb_period", 20)
        self.bb_std:       float = s.get("bb_std", 2.0)
        self.rsi_period:   int   = s.get("rsi_period", 14)
        self.rsi_oversold: float = s.get("rsi_oversold", 28.0)
        self.rsi_overbought: float = s.get("rsi_overbought", 72.0)
        self.adx_period:   int   = s.get("adx_period", 14)
        self.adx_max:      float = s.get("adx_max", 25.0)
        self.volume_mult:  float = s.get("volume_mult", 1.0)

        r = config["risk"]
        self.sl_pct:    float = r.get("sl_pct", 0.012)   # 1.2% hard stop
        self.tp_target: str   = r.get("tp_target", "mid_bb")  # "mid_bb" or "fixed_pct"
        self.tp_pct:    float = r.get("tp_pct", 0.015)   # used if tp_target == "fixed_pct"

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        h, l, c, v = df["high"], df["low"], df["close"], df["volume"]

        bb_up, bb_mid, bb_low = bollinger_bands(c, self.bb_period, self.bb_std)
        df["bb_upper"]  = bb_up
        df["bb_middle"] = bb_mid
        df["bb_lower"]  = bb_low

        df["rsi"]     = rsi(c, self.rsi_period)
        df["adx"]     = adx(h, l, c, self.adx_period)
        df["vol_sma"] = volume_sma(v, 20)
        return df

    def generate_signal(self, df: pd.DataFrame) -> Optional[TradeSetup]:
        df = self.compute_indicators(df)
        min_bars = max(self.bb_period, self.adx_period, self.rsi_period) * 2 + 5
        if len(df) < min_bars:
            return None

        idx = len(df) - 2
        row = df.iloc[idx]

        close    = row["close"]
        bb_up    = row["bb_upper"]
        bb_mid   = row["bb_middle"]
        bb_low   = row["bb_lower"]
        rsi_v    = row["rsi"]
        adx_v    = row["adx"]
        vol      = row["volume"]
        vol_avg  = row["vol_sma"]

        if pd.isna(bb_low) or pd.isna(rsi_v) or pd.isna(adx_v) or pd.isna(vol_avg):
            return None

        # REGIME GATE — only trade in chop, never in trends
        if adx_v >= self.adx_max:
            return None

        if vol < self.volume_mult * vol_avg:
            return None

        # ── LONG (oversold reversion) ────────────────────────────────────────
        if close <= bb_low and rsi_v <= self.rsi_oversold:
            entry     = close
            stop_loss = entry * (1 - self.sl_pct)
            take_profit = bb_mid if self.tp_target == "mid_bb" else entry * (1 + self.tp_pct)
            if take_profit <= entry:
                return None
            return TradeSetup(
                signal=Signal.LONG,
                entry=entry,
                stop_loss=round(stop_loss, 6),
                take_profit=round(take_profit, 6),
                reason=(
                    f"LONG BB-reversion | close={close:.5f}≤BBlow={bb_low:.5f} | "
                    f"RSI={rsi_v:.1f} | ADX={adx_v:.1f} (chop) | TP@mid={bb_mid:.5f}"
                ),
            )

        # ── SHORT (overbought reversion) ─────────────────────────────────────
        if close >= bb_up and rsi_v >= self.rsi_overbought:
            entry     = close
            stop_loss = entry * (1 + self.sl_pct)
            take_profit = bb_mid if self.tp_target == "mid_bb" else entry * (1 - self.tp_pct)
            if take_profit >= entry:
                return None
            return TradeSetup(
                signal=Signal.SHORT,
                entry=entry,
                stop_loss=round(stop_loss, 6),
                take_profit=round(take_profit, 6),
                reason=(
                    f"SHORT BB-reversion | close={close:.5f}≥BBup={bb_up:.5f} | "
                    f"RSI={rsi_v:.1f} | ADX={adx_v:.1f} (chop) | TP@mid={bb_mid:.5f}"
                ),
            )

        return None
