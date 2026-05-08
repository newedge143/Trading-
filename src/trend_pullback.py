"""
Trend-Pullback Strategy (true pullback, NOT breakout)
======================================================
Multi-timeframe: 1h trend filter + 5m entry signal.

LONG entry — all required:
  1. Higher TF (1h) EMA50 sloping up over last 5 candles → uptrend confirmed
  2. 5m close > EMA50 (intermediate uptrend)
  3. PULLBACK happened: any of the last `pullback_lookback` bars had low ≤ EMA20
  4. BOUNCE confirmation: current 5m close > EMA20 (price recaptured the mean)
  5. RSI(14) between 45 and 70 (rising from pullback, not exhausted)
  6. Volume ≥ avg

SHORT mirror.

Exit:
  - Initial stop: low of pullback - 0.5×ATR (or fixed % cap)
  - Take profit: 2.5× initial risk OR trailing stop at EMA20
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from src.indicators import atr, ema, rsi, volume_sma


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


class TrendPullbackStrategy:
    """5m signals filtered by 1h trend, entries on EMA20 pullback+bounce."""

    def __init__(self, config: dict):
        s = config.get("strategy", {})
        self.htf_ema_period: int = s.get("htf_ema_period", 50)
        self.htf_slope_lookback: int = s.get("htf_slope_lookback", 5)
        self.htf_slope_min_pct: float = s.get("htf_slope_min_pct", 0.001)  # 0.1%

        self.ema_fast: int = s.get("ema_fast", 20)
        self.ema_slow: int = s.get("ema_slow", 50)
        self.pullback_lookback: int = s.get("pullback_lookback", 6)   # bars to look back for pullback
        self.pullback_atr_mult: float = s.get("pullback_atr_mult", 0.3)  # how close to EMA20 counts as touch

        self.rsi_period: int = s.get("rsi_period", 14)
        self.rsi_long_min: float = s.get("rsi_long_min", 45)
        self.rsi_long_max: float = s.get("rsi_long_max", 70)
        self.rsi_short_min: float = s.get("rsi_short_min", 30)
        self.rsi_short_max: float = s.get("rsi_short_max", 55)

        self.volume_mult: float = s.get("volume_mult", 1.0)

        r = config["risk"]
        self.atr_sl_mult: float = r.get("atr_sl_mult", 1.5)
        self.rr_ratio: float = r.get("reward_risk_ratio", 2.5)

    def compute_indicators(self, df: pd.DataFrame, htf_df: pd.DataFrame) -> pd.DataFrame:
        """df is 5m, htf_df is 1h. Aligns 1h EMA back to 5m via ffill."""
        df = df.copy()
        h, l, c, v = df["high"], df["low"], df["close"], df["volume"]

        df["ema_fast"] = ema(c, self.ema_fast)
        df["ema_slow"] = ema(c, self.ema_slow)
        df["rsi"]      = rsi(c, self.rsi_period)
        df["atr"]      = atr(h, l, c, 14)
        df["vol_sma"]  = volume_sma(v, 20)

        # Compute HTF EMA on 1h, then forward-fill to 5m timestamps
        htf_ema = ema(htf_df["close"], self.htf_ema_period)
        htf_aligned = htf_ema.reindex(df.index, method="ffill")
        df["htf_ema"] = htf_aligned

        # HTF slope over last N htf bars (in pct terms), aligned to 5m
        htf_slope = htf_ema.pct_change(self.htf_slope_lookback)
        df["htf_slope"] = htf_slope.reindex(df.index, method="ffill")

        return df

    def generate_signal(self, df: pd.DataFrame, htf_df: pd.DataFrame) -> Optional[TradeSetup]:
        df = self.compute_indicators(df, htf_df)
        min_bars = max(self.ema_slow, self.rsi_period, self.pullback_lookback) + 5
        if len(df) < min_bars:
            return None

        idx = len(df) - 2
        if idx < self.pullback_lookback:
            return None

        row = df.iloc[idx]
        close      = row["close"]
        ema_fast   = row["ema_fast"]
        ema_slow   = row["ema_slow"]
        rsi_v      = row["rsi"]
        atr_v      = row["atr"]
        htf_slope  = row["htf_slope"]
        htf_ema_v  = row["htf_ema"]
        vol        = row["volume"]
        vol_avg    = row["vol_sma"]

        if any(pd.isna(x) for x in [close, ema_fast, ema_slow, rsi_v, atr_v, htf_slope, htf_ema_v, vol_avg]):
            return None

        if vol < self.volume_mult * vol_avg:
            return None

        # Pullback window — last N closed bars
        window = df.iloc[idx - self.pullback_lookback : idx]

        # ── LONG: htf uptrend, intermediate uptrend, pullback to EMA20, bounce ──
        htf_up = htf_slope > self.htf_slope_min_pct
        uptrend_intermediate = close > ema_slow
        pullback_long = (window["low"] <= window["ema_fast"] + self.pullback_atr_mult * atr_v).any()
        bounce_long = close > ema_fast and df.iloc[idx - 1]["close"] <= df.iloc[idx - 1]["ema_fast"] * 1.001

        if (
            htf_up
            and uptrend_intermediate
            and pullback_long
            and bounce_long
            and self.rsi_long_min <= rsi_v <= self.rsi_long_max
        ):
            entry = close
            recent_low = float(window["low"].min())
            stop_loss = min(recent_low - 0.2 * atr_v, entry - self.atr_sl_mult * atr_v)
            risk = entry - stop_loss
            if risk <= 0:
                return None
            take_profit = entry + self.rr_ratio * risk
            return TradeSetup(
                signal=Signal.LONG,
                entry=entry,
                stop_loss=round(stop_loss, 6),
                take_profit=round(take_profit, 6),
                reason=(
                    f"LONG pullback | 1h slope={htf_slope*100:.2f}% UP | "
                    f"EMA20={ema_fast:.5f} EMA50={ema_slow:.5f} | RSI={rsi_v:.1f}"
                ),
            )

        # ── SHORT mirror ──
        htf_down = htf_slope < -self.htf_slope_min_pct
        downtrend_intermediate = close < ema_slow
        pullback_short = (window["high"] >= window["ema_fast"] - self.pullback_atr_mult * atr_v).any()
        bounce_short = close < ema_fast and df.iloc[idx - 1]["close"] >= df.iloc[idx - 1]["ema_fast"] * 0.999

        if (
            htf_down
            and downtrend_intermediate
            and pullback_short
            and bounce_short
            and self.rsi_short_min <= rsi_v <= self.rsi_short_max
        ):
            entry = close
            recent_high = float(window["high"].max())
            stop_loss = max(recent_high + 0.2 * atr_v, entry + self.atr_sl_mult * atr_v)
            risk = stop_loss - entry
            if risk <= 0:
                return None
            take_profit = entry - self.rr_ratio * risk
            return TradeSetup(
                signal=Signal.SHORT,
                entry=entry,
                stop_loss=round(stop_loss, 6),
                take_profit=round(take_profit, 6),
                reason=(
                    f"SHORT pullback | 1h slope={htf_slope*100:.2f}% DOWN | "
                    f"EMA20={ema_fast:.5f} EMA50={ema_slow:.5f} | RSI={rsi_v:.1f}"
                ),
            )

        return None
