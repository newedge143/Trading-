"""
Five research agents with structured outputs and an append-only decision log.

These are deterministic, rule-based analytical agents.  They read only the
data slices handed to them by the engine (never the future), they cannot
create prices, and they have no write access to fills or the trade ledger:
the engine owns accounting and only consumes the agents' recommendations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from tradinggym.config import RiskConfig, StrategyConfig
from tradinggym import strategy as strat


def _clean(v):
    if isinstance(v, (float, np.floating)):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, pd.Series):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


class DecisionLog:
    """Append-only JSONL log. Entries are never modified after writing."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self.entries: List[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("")

    def record(self, agent: str, time: pd.Timestamp, output: dict) -> dict:
        entry = {"agent": agent, "decision_time": str(time), "output": _clean(output)}
        self.entries.append(entry)
        if self.path:
            with open(self.path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        return entry


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    eligible: List[str]
    rejected: Dict[str, str]
    unusual_volume: List[str]
    unusual_volatility: List[str]
    quote_volume_24h: Dict[str, float]


class MarketScanner:
    name = "market_scanner"

    def __init__(self, risk: RiskConfig, bars_per_day: int = 24):
        self.risk, self.bpd = risk, bars_per_day

    def scan(self, panel: Dict[str, pd.DataFrame], t: pd.Timestamp,
             log: DecisionLog) -> ScanResult:
        eligible, rejected, uv, uvol, qv = [], {}, [], [], {}
        for sym, df in panel.items():
            strat.assert_no_future(df, t)
            last = df.iloc[-1] if len(df) else None
            if last is None or pd.isna(last["close"]):
                rejected[sym] = "no real bar at decision time"
                continue
            day = df.tail(self.bpd)
            q = float(day["quote_volume"].sum(skipna=True))
            qv[sym] = q
            if q < self.risk.min_quote_volume_24h:
                rejected[sym] = f"24h quote volume {q:,.0f} < minimum"
                continue
            eligible.append(sym)
            hist_vol = df["quote_volume"].tail(self.bpd * 14)
            if len(hist_vol) > self.bpd and day["quote_volume"].mean() > 2 * hist_vol.mean():
                uv.append(sym)
            r = np.log(df["close"]).diff()
            if len(r.dropna()) > self.bpd * 2:
                if r.tail(self.bpd).std() > 2 * r.tail(self.bpd * 14).std():
                    uvol.append(sym)
        res = ScanResult(eligible, rejected, uv, uvol, qv)
        log.record(self.name, t, asdict(res))
        return res

    @staticmethod
    def rank_universe(panel: Dict[str, pd.DataFrame], t: pd.Timestamp, top_n: int = 30,
                      days: int = 30, bars_per_day: int = 24) -> List[str]:
        """Rank candidate perps by trailing quote volume known at time t."""
        vols = {}
        for sym, df in panel.items():
            past = df[df.index <= t].tail(days * bars_per_day)
            vols[sym] = float(past["quote_volume"].sum(skipna=True))
        return [s for s, _ in sorted(vols.items(), key=lambda kv: -kv[1])][:top_n]


class MomentumAnalyst:
    name = "momentum_analyst"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def analyse(self, closes: pd.DataFrame, t: pd.Timestamp, log: DecisionLog) -> dict:
        strat.assert_no_future(closes, t)
        mom = strat.momentum(closes, self.cfg.momentum_lookback_hours)
        ranks = mom.rank(ascending=False)
        out = {"momentum": mom, "rank": ranks,
               "long_candidates": [s for s in mom.index if mom[s] > 0],
               "short_candidates": [s for s in mom.index if mom[s] < 0]}
        log.record(self.name, t, out)
        return out


class FundingAnalyst:
    name = "funding_analyst"

    def __init__(self, cfg: StrategyConfig, risk: RiskConfig):
        self.cfg, self.risk = cfg, risk

    def analyse(self, funding: Dict[str, pd.DataFrame], t: pd.Timestamp,
                log: DecisionLog) -> dict:
        ms = int(t.value // 1_000_000)
        avg = strat.trailing_funding(funding, ms, self.cfg.funding_lookback_events)
        last = strat.trailing_funding(funding, ms, 1)
        out = {"avg_funding": avg, "last_funding": last,
               "est_cost_8h_long": last, "est_cost_8h_short": -last,
               "favourable_long": [s for s in last.index if last[s] < 0],
               "favourable_short": [s for s in last.index if last[s] > 0]}
        log.record(self.name, t, out)
        return out

    def passes_filter(self, sym: str, side: float, last_funding: pd.Series) -> bool:
        rate = last_funding.get(sym, np.nan)
        if not np.isfinite(rate) or side == 0:
            return True
        cost = np.sign(side) * rate  # >0 means we pay
        return cost <= self.risk.funding_filter_8h


@dataclass
class RiskDecision:
    approved_weights: Dict[str, float]
    gross_notional: float
    rejected: Dict[str, str] = field(default_factory=dict)
    halt: bool = False
    reason: str = ""


class RiskManagerAgent:
    name = "risk_manager"

    def __init__(self, risk: RiskConfig, initial_capital: float):
        self.risk, self.initial = risk, initial_capital

    def gross_notional(self, equity: float) -> float:
        base = equity if self.risk.sizing == "equity" else self.initial
        g = self.risk.exposure_utilisation * self.risk.max_leverage * base
        return max(0.0, min(g, self.risk.max_leverage * max(equity, 0.0)))

    def approve(self, weights: pd.Series, equity: float, t: pd.Timestamp,
                log: DecisionLog, eligible: List[str], cooldown: Dict[str, int],
                funding_ok) -> RiskDecision:
        rejected, w = {}, weights.copy()
        for sym in w.index:
            if w[sym] == 0:
                continue
            if sym not in eligible:
                rejected[sym] = "not eligible (scanner)"; w[sym] = 0.0
            elif cooldown.get(sym, 0) > 0:
                rejected[sym] = "stop-loss cooldown"; w[sym] = 0.0
            elif not funding_ok(sym, w[sym]):
                rejected[sym] = "funding cost filter"; w[sym] = 0.0
            elif abs(w[sym]) > self.risk.max_position_weight:
                rejected[sym] = f"weight capped at {self.risk.max_position_weight}"
                w[sym] = np.sign(w[sym]) * self.risk.max_position_weight
        nz = w[w != 0].abs().sort_values(ascending=False)
        for sym in nz.index[self.risk.max_positions:]:
            rejected[sym] = "max simultaneous positions"; w[sym] = 0.0
        g = self.gross_notional(equity)
        dec = RiskDecision({k: float(v) for k, v in w.items()}, g, rejected)
        log.record(self.name, t, asdict(dec) | {"equity": equity})
        return dec


class StrategyEvaluator:
    """Version registry + overfitting guard for parameter changes."""

    name = "strategy_evaluator"

    def __init__(self, log: DecisionLog):
        self.log, self.versions = log, []

    def register(self, params: dict, label: str) -> str:
        vid = hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:10]
        self.versions.append({"version": vid, "label": label, "params": params})
        self.log.record(self.name, pd.Timestamp.now(tz="UTC"),
                        {"event": "register", "version": vid, "label": label, "params": params})
        return vid

    def accept_change(self, baseline: dict, candidate: dict, t=None) -> dict:
        """baseline/candidate: {'train_sharpe','valid_sharpe','valid_return'}.

        Reject when the in-sample improvement does not carry out-of-sample:
        candidate must beat baseline on validation, have positive validation
        Sharpe, and keep >= 50 % of its training Sharpe.
        """
        reasons = []
        if candidate["valid_sharpe"] <= baseline["valid_sharpe"]:
            reasons.append("does not beat baseline out-of-sample")
        if candidate["valid_sharpe"] <= 0:
            reasons.append("non-positive validation Sharpe")
        if candidate["train_sharpe"] > 0 and candidate["valid_sharpe"] < 0.5 * candidate["train_sharpe"]:
            reasons.append("validation Sharpe < 50% of training Sharpe (overfit)")
        out = {"event": "parameter_review", "accepted": not reasons, "reasons": reasons,
               "baseline": baseline, "candidate": candidate}
        self.log.record(self.name, t or pd.Timestamp.now(tz="UTC"), out)
        return out

    def compare(self, expected: dict, actual: dict, t=None) -> dict:
        deteriorating = (actual.get("sharpe", 0) < expected.get("sharpe", 0) * 0.5
                         or actual.get("max_drawdown", 0) > 1.5 * max(expected.get("max_drawdown", 0), 1e-9))
        out = {"event": "expectation_check", "expected": expected, "actual": actual,
               "deteriorating": bool(deteriorating)}
        self.log.record(self.name, t or pd.Timestamp.now(tz="UTC"), out)
        return out
