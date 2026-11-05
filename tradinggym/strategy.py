"""
Crypto Momentum + Funding Rate cross-sectional strategy (research version).

Every function here receives ONLY data whose timestamps are <= the decision
time.  The engine enforces this by slicing; `assert_no_future` double-checks.

Signal definition (documented so it can be compared to the publication):
  momentum_i  = close_i(t) / close_i(t - L) - 1            L = 14 days of bars
  funding_i   = mean of the last N realised funding rates with ts <= t
  carry_i     = -funding_i      (high positive funding = crowded longs; a short
                                 *receives* funding, so negative carry for longs)
  score_i     = w_m * z(momentum)_i + w_f * z(carry)_i    (cross-sectional z)
  weight_i    = score_i / sum_j |score_j|                   (dollar-neutral-ish,
                                                             gross = 1)
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from tradinggym.config import StrategyConfig


def assert_no_future(df_or_series, decision_time: pd.Timestamp) -> None:
    idx = df_or_series.index
    if len(idx) and idx.max() > decision_time:
        raise AssertionError(f"look-ahead: data at {idx.max()} > decision time {decision_time}")


def momentum(closes: pd.DataFrame, lookback: int) -> pd.Series:
    """Trailing return over `lookback` bars using the last available row."""
    if len(closes) <= lookback:
        return pd.Series(np.nan, index=closes.columns)
    now, then = closes.iloc[-1], closes.iloc[-1 - lookback]
    return now / then - 1.0


def trailing_funding(funding: Dict[str, pd.DataFrame], decision_ms: int,
                     n_events: int) -> pd.Series:
    out = {}
    for sym, df in funding.items():
        if df is None or df.empty:
            out[sym] = np.nan
            continue
        past = df[df["timestamp"] <= decision_ms]
        out[sym] = past["funding_rate"].tail(n_events).mean() if len(past) else np.nan
    return pd.Series(out, dtype=float)


def zscore(s: pd.Series) -> pd.Series:
    s = s.astype(float)
    valid = s.dropna()
    if len(valid) < 2 or valid.std(ddof=0) == 0:
        return pd.Series(0.0, index=s.index).where(s.notna())
    return (s - valid.mean()) / valid.std(ddof=0)


def combine_scores(mom: pd.Series, carry: pd.Series, cfg: StrategyConfig) -> pd.Series:
    if mom.notna().sum() == 1:  # single market: time-series signs, no cross-section
        c = carry.reindex(mom.index).fillna(0.0)
        return (cfg.momentum_weight * np.sign(mom) + cfg.funding_weight * np.sign(c)).where(mom.notna())
    zm, zc = zscore(mom), zscore(carry.reindex(mom.index))
    zc = zc.fillna(0.0).where(zm.notna())
    return cfg.momentum_weight * zm + cfg.funding_weight * zc


def scores_to_weights(scores: pd.Series, cfg: StrategyConfig,
                      max_weight: float = 1.0) -> pd.Series:
    s = scores.dropna()
    w = pd.Series(0.0, index=scores.index)
    if s.empty:
        return w
    if len(s) == 1:  # single market: time-series sign
        w[s.index[0]] = float(np.sign(s.iloc[0]))
        return w
    if not cfg.allow_short:
        s = s.clip(lower=0.0)
    gross = s.abs().sum()
    if gross == 0:
        return w
    w.loc[s.index] = (s / gross).clip(-max_weight, max_weight)
    return w


def target_weights(closes: pd.DataFrame, funding: Dict[str, pd.DataFrame],
                   decision_time: pd.Timestamp, cfg: StrategyConfig,
                   max_weight: float = 1.0) -> dict:
    """Full signal pipeline; returns a structured dict for logging."""
    assert_no_future(closes, decision_time)
    decision_ms = int(decision_time.value // 1_000_000)
    mom = momentum(closes, cfg.momentum_lookback_hours)
    fund = trailing_funding(funding, decision_ms, cfg.funding_lookback_events).reindex(mom.index)
    scores = combine_scores(mom, -fund, cfg)
    weights = scores_to_weights(scores, cfg, max_weight)
    return {"momentum": mom, "funding": fund, "scores": scores, "weights": weights}


def weight_drift(current: pd.Series, target: pd.Series) -> float:
    """Relative drift: sum|w_cur - w_tgt| / sum|w_tgt|  (inf if target empty
    but holdings exist; 0 if both empty)."""
    cur = current.reindex(target.index.union(current.index)).fillna(0.0)
    tgt = target.reindex(cur.index).fillna(0.0)
    denom = tgt.abs().sum()
    num = (cur - tgt).abs().sum()
    if denom == 0:
        return 0.0 if num == 0 else float("inf")
    return float(num / denom)
