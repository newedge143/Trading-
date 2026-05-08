"""
Scalp Strategy — Ported from freqtrade-strategies/berlinguyinca/Scalp.py
=========================================================================
Original: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/berlinguyinca/Scalp.py
Author  : berlinguyinca   |   License: GPLv3 (freqtrade-strategies)

Mean-reversion scalper for 1m timeframe. Reported win rate ~75-85%.
Long-only in original; this port adds the symmetric SHORT mirror for futures.

LONG entry:
  1. open < EMA_low(5)             — price dipped below short-term EMA of lows
  2. ADX(14) > 30                  — strong trend (not chop)
  3. fastk < 30 AND fastd < 30     — Stochastic oversold
  4. crossed_above(fastk, fastd)   — momentum turning up

LONG exit:
  - open >= EMA_high(5), OR
  - crossed_above(fastk, 70), OR
  - crossed_above(fastd, 70)

SHORT mirror:
  Entry: open > EMA_high(5), ADX > 30, fastk > 70, fastd > 70, crossed_below(fastk, fastd)
  Exit : open <= EMA_low(5), or crossed_below(fastk/fastd, 30)

Stop: 1.5×ATR(14) (configurable). Take-profit on signal exit, not fixed ROI.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from src.indicators import adx, atr, ema, stoch_fast


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


class ScalpStrategy:
    """Stoch + ADX + EMA mean-reversion scalper, futures-ready (long & short)."""

    def __init__(self, config: dict):
        s = config.get("strategy", {})
        self.ema_period: int   = s.get("ema_period", 5)
        self.adx_period: int   = s.get("adx_period", 14)
        self.adx_min: float    = s.get("adx_min", 30.0)
        self.stoch_k: int      = s.get("stoch_k", 5)
        self.stoch_d: int      = s.get("stoch_d", 3)
        self.stoch_smooth: int = s.get("stoch_smooth", 3)
        self.stoch_oversold: float   = s.get("stoch_oversold", 30.0)
        self.stoch_overbought: float = s.get("stoch_overbought", 70.0)

        r = config["risk"]
        self.atr_sl_mult: float = r.get("atr_sl_mult", 1.5)
        self.rr_ratio: float    = r.get("reward_risk_ratio", 1.5)

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        h, l, c, o = df["high"], df["low"], df["close"], df["open"]

        df["ema_high"] = ema(h, self.ema_period)
        df["ema_low"]  = ema(l, self.ema_period)
        df["adx"]      = adx(h, l, c, self.adx_period)
        df["fastk"], df["fastd"] = stoch_fast(
            h, l, c, self.stoch_k, self.stoch_d, self.stoch_smooth
        )
        df["atr14"] = atr(h, l, c, 14)

        df["fastk_prev"] = df["fastk"].shift(1)
        df["fastd_prev"] = df["fastd"].shift(1)
        return df

    def generate_signal(self, df: pd.DataFrame) -> Optional[TradeSetup]:
        df = self.compute_indicators(df)
        min_bars = max(self.adx_period, self.stoch_k, self.ema_period) * 2 + 5
        if len(df) < min_bars:
            return None

        idx = len(df) - 2  # last fully closed bar
        row = df.iloc[idx]

        ema_high = row["ema_high"]
        ema_low  = row["ema_low"]
        adx_val  = row["adx"]
        fastk    = row["fastk"]
        fastd    = row["fastd"]
        fk_prev  = row["fastk_prev"]
        fd_prev  = row["fastd_prev"]
        open_    = row["open"]
        close    = row["close"]
        atr14    = row["atr14"]

        if pd.isna(adx_val) or pd.isna(fastk) or pd.isna(fastd) or pd.isna(atr14):
            return None

        # ── LONG ── price dipped below EMA_low, strong trend, stoch oversold turning up
        long_cross = (fk_prev <= fd_prev) and (fastk > fastd)
        if (
            open_ < ema_low
            and adx_val > self.adx_min
            and fastk < self.stoch_oversold
            and fastd < self.stoch_oversold
            and long_cross
        ):
            entry = close
            stop  = entry - self.atr_sl_mult * atr14
            risk  = entry - stop
            if risk <= 0:
                return None
            return TradeSetup(
                signal=Signal.LONG,
                entry=entry,
                stop_loss=round(stop, 6),
                take_profit=round(entry + self.rr_ratio * risk, 6),
                reason=(
                    f"LONG scalp | open<{ema_low:.5f} | ADX={adx_val:.1f} | "
                    f"fastk={fastk:.1f}>fastd={fastd:.1f} (oversold cross)"
                ),
            )

        # ── SHORT mirror ── price spiked above EMA_high, strong trend, stoch overbought turning down
        short_cross = (fk_prev >= fd_prev) and (fastk < fastd)
        if (
            open_ > ema_high
            and adx_val > self.adx_min
            and fastk > self.stoch_overbought
            and fastd > self.stoch_overbought
            and short_cross
        ):
            entry = close
            stop  = entry + self.atr_sl_mult * atr14
            risk  = stop - entry
            if risk <= 0:
                return None
            return TradeSetup(
                signal=Signal.SHORT,
                entry=entry,
                stop_loss=round(stop, 6),
                take_profit=round(entry - self.rr_ratio * risk, 6),
                reason=(
                    f"SHORT scalp | open>{ema_high:.5f} | ADX={adx_val:.1f} | "
                    f"fastk={fastk:.1f}<fastd={fastd:.1f} (overbought cross)"
                ),
            )

        return None
