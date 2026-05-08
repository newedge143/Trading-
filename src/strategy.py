"""
Acceleration Breakout Strategy — XAUUSD.P Optimised
-----------------------------------------------------
Research sources applied:
  • Forex Factory NY ORB: candle body quality filter, 1-bar confirmation, session gate
  • ilahuerta-IA backtrader XAUUSD: 2.5×ATR stop, 3:1 R/R, 1% risk
  • GOLD_ORB EA: max 2 trades/day, 3:1 R/R (1200pt/400pt)
  • TrendSpider / QuantifiedStrategies: BB(20,2) inside KC(20,1.5), 1.5× volume

Signal logic:
  1. Session gate  — only trade London (07:00–09:30 UTC) or NY open (13:30–15:30 UTC)
  2. Squeeze       — BB inside KC for ≥ min_squeeze_bars consecutive bars
  3. Trend filter  — close above/below 50 EMA sets allowed direction
  4. Breakout      — close outside shifted consolidation range
  5. Body quality  — breakout candle body ≥ 0.8×ATR(5) AND ≥ 60% of candle range
  6. Momentum      — fast ROC above its rolling baseline (acceleration)
  7. Volume        — ≥ 1.5× 20-bar average
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

import pandas as pd

from src.indicators import (
    atr,
    bb_squeeze,
    candle_body_ratio,
    consolidation_range,
    ema,
    momentum_histogram,
    squeeze_duration,
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
    """Convert [{'start':'07:00','end':'09:30'}, ...] to list of (int, int) minute-of-day tuples."""
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

        # Consolidation
        self.consol_period: int = s["consolidation_period"]
        self.consol_atr_mult: float = s["consolidation_atr_mult"]
        self.bb_period: int = s["squeeze_bb_period"]
        self.bb_std: float = s["squeeze_bb_std"]
        self.kc_mult: float = s["squeeze_kc_mult"]
        self.min_squeeze_bars: int = s.get("min_squeeze_bars", 3)

        # Breakout candle quality (Forex Factory NY ORB)
        self.body_atr_mult: float = s.get("breakout_body_atr_mult", 0.8)
        self.body_candle_ratio: float = s.get("breakout_body_candle_ratio", 0.60)

        # Momentum
        self.roc_fast: int = s["roc_fast"]
        self.roc_slow: int = s["roc_slow"]
        self.accel_threshold: float = s["accel_threshold"]

        # Confirmation
        self.breakout_bars: int = s["breakout_bars"]
        self.volume_mult: float = s["volume_mult"]

        # Trend filter
        self.ema_period: int = s.get("ema_trend_period", 50)

        # Risk
        r = config["risk"]
        self.rr_ratio: float = r["reward_risk_ratio"]
        self.atr_sl_mult: float = r.get("atr_sl_mult", 2.5)

    # ──────────────────────────────────────────────────────────────────────────
    # Indicator computation
    # ──────────────────────────────────────────────────────────────────────────

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        h, l, c, o, v = df["high"], df["low"], df["close"], df["open"], df["volume"]

        # Consolidation range (shifted so close can exceed it)
        in_consol, roll_high, roll_low = consolidation_range(
            h, l, c, self.consol_period, self.consol_atr_mult
        )
        squeeze = bb_squeeze(h, l, c, self.bb_period, self.bb_std, self.kc_mult)
        sq_dur = squeeze_duration(squeeze)

        df["in_consol"] = in_consol | squeeze
        df["consol_high"] = roll_high
        df["consol_low"] = roll_low
        df["squeeze"] = squeeze
        df["squeeze_dur"] = sq_dur

        # Momentum acceleration
        df["accel"] = momentum_histogram(c, self.roc_fast, self.roc_slow)

        # ATR (two periods)
        df["atr14"] = atr(h, l, c, 14)
        df["atr5"] = atr(h, l, c, 5)

        # Candle body quality
        body, ratio = candle_body_ratio(o, c, h, l)
        df["body_size"] = body
        df["body_ratio"] = ratio

        # Volume filter
        df["vol_sma"] = volume_sma(v, 20)

        # Trend filter: 50 EMA
        df["ema50"] = ema(c, self.ema_period)

        return df

    # ──────────────────────────────────────────────────────────────────────────
    # Filter helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _in_session(self, ts: pd.Timestamp) -> bool:
        if not self.session_filter or not self.sessions:
            return True
        minute = ts.hour * 60 + ts.minute
        return any(start <= minute < end for start, end in self.sessions)

    def _breakout_confirmed(self, df: pd.DataFrame, idx: int, direction: Signal) -> bool:
        if idx < self.breakout_bars:
            return False
        rows = df.iloc[idx - self.breakout_bars + 1 : idx + 1]
        if direction == Signal.LONG:
            return all(rows["close"] > rows["consol_high"])
        return all(rows["close"] < rows["consol_low"])

    def _body_quality_ok(self, row: pd.Series) -> bool:
        """Breakout candle body ≥ 0.8×ATR(5) AND ≥ 60% of candle range."""
        return (
            row["body_size"] >= self.body_atr_mult * row["atr5"]
            and row["body_ratio"] >= self.body_candle_ratio
        )

    def _volume_ok(self, row: pd.Series) -> bool:
        return row["volume"] > self.volume_mult * row["vol_sma"]

    # ──────────────────────────────────────────────────────────────────────────
    # Signal generation
    # ──────────────────────────────────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Optional[TradeSetup]:
        df = self.compute_indicators(df)

        min_bars = max(self.consol_period, self.bb_period, self.roc_slow, self.ema_period) + 5
        if len(df) < min_bars:
            return None

        idx = len(df) - 2   # last fully closed bar
        row = df.iloc[idx]

        # 1. Session gate
        if not self._in_session(row.name):
            return None

        # 2. Recent squeeze (min_squeeze_bars consecutive bars prior to breakout)
        lookback = df["squeeze_dur"].iloc[: idx + 1]
        max_recent_squeeze = lookback.iloc[max(0, idx - self.consol_period) : idx].max()
        if max_recent_squeeze < self.min_squeeze_bars:
            return None

        # 3. Extract price levels
        accel = row["accel"]
        consol_high = row["consol_high"]
        consol_low = row["consol_low"]
        close = row["close"]
        atr14 = row["atr14"]
        ema50 = row["ema50"]

        # 4. LONG setup
        if (
            close > consol_high                                    # breakout above range
            and close > ema50                                      # above 50 EMA (trend up)
            and accel > self.accel_threshold                       # momentum accelerating
            and self._breakout_confirmed(df, idx, Signal.LONG)    # candle closed outside
            and self._body_quality_ok(row)                         # strong body, no wick trap
            and self._volume_ok(row)                               # volume spike
        ):
            entry = close
            stop_loss = consol_low - self.atr_sl_mult * 0.2 * atr14   # buffer below range
            risk = entry - stop_loss
            if risk <= 0:
                return None
            return TradeSetup(
                signal=Signal.LONG,
                entry=entry,
                stop_loss=round(stop_loss, 4),
                take_profit=round(entry + self.rr_ratio * risk, 4),
                reason=(
                    f"LONG breakout above {consol_high:.2f} | "
                    f"squeeze={int(max_recent_squeeze)}bars | "
                    f"accel={accel:.3f} | ema50={ema50:.2f}"
                ),
            )

        # 5. SHORT setup
        if (
            close < consol_low                                     # breakdown below range
            and close < ema50                                      # below 50 EMA (trend down)
            and accel < -self.accel_threshold                      # momentum accelerating down
            and self._breakout_confirmed(df, idx, Signal.SHORT)
            and self._body_quality_ok(row)
            and self._volume_ok(row)
        ):
            entry = close
            stop_loss = consol_high + self.atr_sl_mult * 0.2 * atr14
            risk = stop_loss - entry
            if risk <= 0:
                return None
            return TradeSetup(
                signal=Signal.SHORT,
                entry=entry,
                stop_loss=round(stop_loss, 4),
                take_profit=round(entry - self.rr_ratio * risk, 4),
                reason=(
                    f"SHORT breakdown below {consol_low:.2f} | "
                    f"squeeze={int(max_recent_squeeze)}bars | "
                    f"accel={accel:.3f} | ema50={ema50:.2f}"
                ),
            )

        return None
