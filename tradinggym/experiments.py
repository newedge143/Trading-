"""
Experiment orchestration: leverage grid, sizing modes, stress tests and the
walk-forward EXPERIMENTAL mode.  Each experiment starts from the configured
initial capital; nothing is compounded across experiments.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from tradinggym.agents import DecisionLog, StrategyEvaluator
from tradinggym.config import (LEVERAGE_EXPERIMENTS, ContractSpec, CostConfig, RiskConfig,
                               RunConfig, StrategyConfig, default_spec)
from tradinggym.engine import Backtester, BacktestResult
from tradinggym import metrics as M

# Walk-forward windows (all strictly before the 48h test window).
TRAIN_WINDOW = ("2026-09-22T00:00:00Z", "2026-10-19T00:00:00Z")
VALID_WINDOW = ("2026-10-19T00:00:00Z", "2026-11-02T00:00:00Z")
FETCH_START = "2026-08-25T00:00:00Z"   # >= 21d warm-up before TRAIN_WINDOW


def build_specs(raw: Dict[str, dict], symbols: List[str]) -> Dict[str, ContractSpec]:
    out = {}
    for s in symbols:
        base = default_spec(s)
        r = raw.get(s) or {}
        out[s] = replace(base, **{k: v for k, v in r.items()
                                  if k in ("tick_size", "lot_step", "min_qty", "min_notional",
                                           "maintenance_margin_rate")})
    return out


def run_one(run: RunConfig, klines, funding, specs, log_path=None,
            start=None, end=None) -> BacktestResult:
    bt = Backtester(run, klines, funding, specs, log_path)
    return bt.run_backtest(start, end)


def leverage_grid(base: RunConfig, klines, funding, specs, log_dir: Path) -> List[dict]:
    rows = []
    for sizing in ("equity", "fixed_notional"):
        for exp, lev in LEVERAGE_EXPERIMENTS.items():
            run = copy.deepcopy(base)
            run.risk = replace(run.risk, max_leverage=lev, sizing=sizing)
            name = f"{exp}_{int(lev)}x_{sizing}"
            res = run_one(run, klines, funding, specs, log_dir / f"{name}.jsonl")
            rows.append({"experiment": exp, "leverage": lev, "sizing": sizing,
                         "scenario": "normal", "name": name, "result": res,
                         "metrics": M.summarize(res)})
    # stress: 2x slippage (and spread), 2x fees, adverse funding
    for exp, lev in LEVERAGE_EXPERIMENTS.items():
        run = copy.deepcopy(base)
        run.risk = replace(run.risk, max_leverage=lev, sizing="equity")
        run.costs = replace(run.costs, fee_multiplier=2.0, slippage_multiplier=2.0,
                            spread_multiplier=2.0, adverse_funding=True)
        name = f"{exp}_{int(lev)}x_equity_STRESS"
        res = run_one(run, klines, funding, specs, log_dir / f"{name}.jsonl")
        rows.append({"experiment": exp, "leverage": lev, "sizing": "equity",
                     "scenario": "stress", "name": name, "result": res,
                     "metrics": M.summarize(res)})
    return rows


def _sharpe_of(res: BacktestResult) -> float:
    s = M.sharpe(res.equity["equity"].pct_change())
    return 0.0 if not np.isfinite(s) else s


def walk_forward(base: RunConfig, klines, funding, specs, log_dir: Path) -> dict:
    """Optimise on TRAIN, validate on VALID, apply once to the 48h TEST."""
    log = DecisionLog(log_dir / "strategy_evaluator.jsonl")
    ev = StrategyEvaluator(log)
    run1 = copy.deepcopy(base)
    run1.risk = replace(run1.risk, max_leverage=1.0, sizing="equity")
    grid = list(itertools.product([7 * 24, 14 * 24, 21 * 24], [0.5, 0.7, 0.9], [0.1, 0.2, 0.3]))
    table = []
    for lb, wm, drift in grid:
        r = copy.deepcopy(run1)
        r.strategy = replace(r.strategy, mode="experimental", momentum_lookback_hours=lb,
                             momentum_weight=wm, funding_weight=round(1 - wm, 2),
                             rebalance_drift=drift)
        try:
            tr = run_one(r, klines, funding, specs, None, *TRAIN_WINDOW)
            va = run_one(r, klines, funding, specs, None, *VALID_WINDOW)
        except ValueError as exc:
            table.append({"lookback_h": lb, "mom_w": wm, "drift": drift, "error": str(exc)})
            continue
        table.append({"lookback_h": lb, "mom_w": wm, "drift": drift,
                      "train_sharpe": _sharpe_of(tr), "valid_sharpe": _sharpe_of(va),
                      "train_return": M.summarize(tr)["return_pct"],
                      "valid_return": M.summarize(va)["return_pct"]})
    df = pd.DataFrame(table)
    base_row = df[(df.lookback_h == 14 * 24) & (df.mom_w == 0.7) & (df.drift == 0.2)].iloc[0]
    best = df.dropna(subset=["train_sharpe"]).sort_values("train_sharpe", ascending=False).iloc[0]
    params = {k: (float(best[k]) if k != "lookback_h" else int(best[k]))
              for k in ("lookback_h", "mom_w", "drift")}
    ev.register({"lookback_h": 336, "mom_w": 0.7, "drift": 0.2}, "baseline")
    ev.register(params, "train-optimised candidate")
    review = ev.accept_change(
        {"train_sharpe": base_row.train_sharpe, "valid_sharpe": base_row.valid_sharpe,
         "valid_return": base_row.valid_return},
        {"train_sharpe": best.train_sharpe, "valid_sharpe": best.valid_sharpe,
         "valid_return": best.valid_return})
    chosen = params if review["accepted"] else {"lookback_h": 336, "mom_w": 0.7, "drift": 0.2}
    return {"grid": df, "candidate": params, "review": review, "chosen": chosen}


def liquidation_probabilities(rows, klines, specs, test_start: str) -> Dict[str, float]:
    """Model estimate per leverage using pre-test returns and the average
    absolute target weights actually used in the equity-sized run."""
    t0 = pd.Timestamp(test_start)
    rets = {}
    for s, df in klines.items():
        d = df.copy()
        d.index = pd.to_datetime(d["timestamp"], unit="ms", utc=True)
        d = d[d.index < t0]
        rets[s] = d["close"].pct_change()
    rets = pd.DataFrame(rets).dropna().tail(24 * 60)
    out = {}
    for row in rows:
        if row["scenario"] != "normal" or row["sizing"] != "equity":
            continue
        wh = pd.DataFrame(row["result"].weights_history)
        if wh.empty:
            out[row["name"]] = float("nan"); continue
        w = {c[2:]: float(wh[c].mean()) for c in wh.columns if c.startswith("w_")}
        if sum(abs(v) for v in w.values()) == 0:
            out[row["name"]] = 0.0; continue
        mmr = {s: specs[s].maintenance_margin_rate for s in w}
        out[row["name"]] = M.liquidation_probability(rets, w, row["leverage"], mmr)
    return out
