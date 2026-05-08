"""
Trend-Aligned Momentum Strategy
---------------------------------
Signal logic — all conditions must align before entry:

  1. Macro trend   — close vs EMA200 (3h+ trend on 1m) sets allowed direction
  2. Momentum      — close vs EMA50 confirms intermediate trend aligned
  3. RSI filter    — RSI(14) in 50-75 for LONG, 25-50 for SHORT (not exhausted)
  4. Breakout      — close > 10-bar rolling high (LONG) or < 10-bar rolling low (SHORT)
  5. Body quality  — breakout candle body ≥ 0.5×ATR(14) AND ≥ 50% of candle range
  6. Volume        — ≥ 1.3× 20-bar average
  7. Acceleration  — fast ROC above its baseline (momentum accelerating)

Stop: 1.5×ATR(14) below entry low (tight — exit fast when wrong)
TP  : 3×risk (3:1 R:R)
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

import pandas as pd

from src.indicators import (
    atr,
    candle_body_ratio,
    consolidation_range,
    ema,
    momentum_histogram,
    rsi,
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


def _parse_sessions(sessions: list) -> List[tuple]:
    out = []
    for s in sessions:
        sh, sm = map(int, s["start"].split(":"))
        eh, em = map(int, s["end"].split(":"))
        out.append((sh * 60 + sm, eh * 60 + em))
    return out


class AccelerationBreakoutStrategy:
    def __init__(self, config: dict):
        s = config["strategy"]

        # Session filter
        self.session_filter: bool = s.get("session_filter", False)
        self.sessions = _parse_sessions(s.get("sessions", []))

        # Breakout detection window
        self.consol_period: int = s["consolidation_period"]
        self.consol_atr_mult: float = s["consolidation_atr_mult"]

        # Trend EMAs
        self.ema_fast: int = s.get("ema_fast_period", 50)
        self.ema_slow: int = s.get("ema_slow_period", 200)

        # RSI filter bounds
        self.rsi_period: int = s.get("rsi_period", 14)
        self.rsi_long_min: float = s.get("rsi_long_min", 50)
        self.rsi_long_max: float = s.get("rsi_long_max", 75)
        self.rsi_short_min: float = s.get("rsi_short_min", 25)
        self.rsi_short_max: float = s.get("rsi_short_max", 50)

        # Candle quality
        self.body_atr_mult: float = s.get("breakout_body_atr_mult", 0.5)
        self.body_candle_ratio: float = s.get("breakout_body_candle_ratio", 0.50)

        # Momentum
        self.roc_fast: int = s["roc_fast"]
        self.roc_slow: int = s["roc_slow"]
        self.accel_threshold: float = s["accel_threshold"]

        # Volume
        self.volume_mult: float = s["volume_mult"]

        # Risk
        r = config["risk"]
        self.rr_ratio: float = r["reward_risk_ratio"]
        self.atr_sl_mult: float = r.get("atr_sl_mult", 1.5)

        # Keep these for backtest_pair_24h.py compatibility
        self.bb_period: int = s.get("squeeze_bb_period", 20)
        self.bb_std: float = s.get("squeeze_bb_std", 2.0)
        self.kc_mult: float = s.get("squeeze_kc_mult", 1.5)
        self.min_squeeze_bars: int = s.get("min_squeeze_bars", 3)
        self.require_squeeze: bool = s.get("require_squeeze", False)
        self.breakout_bars: int = s.get("breakout_bars", 1)

    # ──────────────────────────────────────────────────────────────────────────

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        h, l, c, o, v = df["high"], df["low"], df["close"], df["open"], df["volume"]

        # Breakout levels (shifted — current close can exceed them)
        _, roll_high, roll_low = consolidation_range(
            h, l, c, self.consol_period, self.consol_atr_mult
        )
        df["consol_high"] = roll_high
        df["consol_low"] = roll_low

        # Trend EMAs
        df["ema_fast"] = ema(c, self.ema_fast)
        df["ema_slow"] = ema(c, self.ema_slow)

        # RSI
        df["rsi14"] = rsi(c, self.rsi_period)

        # ATR
        df["atr14"] = atr(h, l, c, 14)

        # Candle body quality
        body, ratio = candle_body_ratio(o, c, h, l)
        df["body_size"] = body
        df["body_ratio"] = ratio

        # Volume
        df["vol_sma"] = volume_sma(v, 20)

        # Momentum acceleration
        df["accel"] = momentum_histogram(c, self.roc_fast, self.roc_slow)

        return df

    # ──────────────────────────────────────────────────────────────────────────

    def _in_session(self, ts: pd.Timestamp) -> bool:
        if not self.session_filter or not self.sessions:
            return True
        minute = ts.hour * 60 + ts.minute
        return any(start <= minute < end for start, end in self.sessions)

    def _body_quality_ok(self, row: pd.Series) -> bool:
        return (
            row["body_size"] >= self.body_atr_mult * row["atr14"]
            and row["body_ratio"] >= self.body_candle_ratio
        )

    def _volume_ok(self, row: pd.Series) -> bool:
        return row["volume"] > self.volume_mult * row["vol_sma"]

    # ──────────────────────────────────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Optional[TradeSetup]:
        df = self.compute_indicators(df)

        min_bars = self.ema_slow + 10
        if len(df) < min_bars:
            return None

        idx = len(df) - 2   # last fully closed bar
        row = df.iloc[idx]

        if not self._in_session(row.name):
            return None

        close       = row["close"]
        atr14       = row["atr14"]
        ema_fast    = row["ema_fast"]
        ema_slow    = row["ema_slow"]
        rsi14       = row["rsi14"]
        consol_high = row["consol_high"]
        consol_low  = row["consol_low"]
        accel       = row["accel"]

        # ── LONG ──────────────────────────────────────────────────────────────
        if (
            close > ema_slow                                  # macro uptrend (EMA200)
            and close > ema_fast                              # intermediate uptrend (EMA50)
            and self.rsi_long_min <= rsi14 <= self.rsi_long_max  # RSI confirms, not overbought
            and close > consol_high                           # price broke above range
            and accel > self.accel_threshold                  # momentum accelerating up
            and self._body_quality_ok(row)                    # strong candle body
            and self._volume_ok(row)                          # volume confirming
        ):
            entry     = close
            stop_loss = entry - self.atr_sl_mult * atr14
            risk      = entry - stop_loss
            if risk <= 0:
                return None
            return TradeSetup(
                signal=Signal.LONG,
                entry=entry,
                stop_loss=round(stop_loss, 6),
                take_profit=round(entry + self.rr_ratio * risk, 6),
                reason=(
                    f"LONG | close>{consol_high:.5f} | "
                    f"EMA50={ema_fast:.5f} EMA200={ema_slow:.5f} | "
                    f"RSI={rsi14:.1f} accel={accel:.4f}"
                ),
            )

        # ── SHORT ─────────────────────────────────────────────────────────────
        if (
            close < ema_slow                                   # macro downtrend (EMA200)
            and close < ema_fast                               # intermediate downtrend (EMA50)
            and self.rsi_short_min <= rsi14 <= self.rsi_short_max  # RSI confirms, not oversold
            and close < consol_low                             # price broke below range
            and accel < -self.accel_threshold                  # momentum accelerating down
            and self._body_quality_ok(row)                     # strong candle body
            and self._volume_ok(row)                           # volume confirming
        ):
            entry     = close
            stop_loss = entry + self.atr_sl_mult * atr14
            risk      = stop_loss - entry
            if risk <= 0:
                return None
            return TradeSetup(
                signal=Signal.SHORT,
                entry=entry,
                stop_loss=round(stop_loss, 6),
                take_profit=round(entry - self.rr_ratio * risk, 6),
                reason=(
                    f"SHORT | close<{consol_low:.5f} | "
                    f"EMA50={ema_fast:.5f} EMA200={ema_slow:.5f} | "
                    f"RSI={rsi14:.1f} accel={accel:.4f}"
                ),
            )

        return None
