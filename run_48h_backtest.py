#!/usr/bin/env python3
"""
TradingGym 48-hour backtest runner.

  python run_48h_backtest.py                       # download from Binance, run, report
  python run_48h_backtest.py --offline             # use CSVs already in data/historical
  python run_48h_backtest.py --exchange bybit      # cross-exchange comparison run

Simulation only.  This script contains no order-routing code.

Exit codes: 0 = executed; 2 = required real data unavailable (nothing simulated).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tradinggym import config as CFG
from tradinggym import data as D
from tradinggym import experiments as X
from tradinggym import metrics as M
from tradinggym import reporting as R


def write_config(run: CFG.RunConfig, reports: Path, extra: dict):
    d = run.to_dict()
    d["experiments"] = {k: {"max_leverage": v} for k, v in CFG.LEVERAGE_EXPERIMENTS.items()}
    d["sizing_modes"] = ["equity", "fixed_notional"]
    d["stress_test"] = {"fee_multiplier": 2.0, "slippage_multiplier": 2.0,
                        "spread_multiplier": 2.0, "adverse_funding": True}
    d["walk_forward"] = {"train": X.TRAIN_WINDOW, "validation": X.VALID_WINDOW,
                         "test": [run.start, run.end]}
    d["contract_specs_defaults"] = {k: v.__dict__ for k, v in CFG.DEFAULT_SPECS.items()}
    d["reproduction_status"] = (
        "UNVERIFIED: parameters (14d momentum, 70/30 momentum/funding, 20% drift "
        "rebalance) are the proposed configuration from the task brief. The original "
        "publication was not retrieved, so this is NOT claimed to be an exact reproduction.")
    d.update(extra)
    (reports / "STRATEGY_CONFIGURATION.json").write_text(json.dumps(d, indent=2, default=str))


def not_executed_report(reports: Path, manifest: dict, run: CFG.RunConfig):
    R.write_empty_outputs(reports)
    lines = [
        "# 48-Hour Backtest — NOT EXECUTED",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "**Status: the backtest did not run, because the required historical market data "
        "could not be retrieved.** No prices, trades or results were simulated or substituted.",
        "",
        f"- Requested test window: `{run.start}` → `{run.end}` (exclusive)",
        f"- Primary exchange: `{manifest.get('exchange')}` ({manifest.get('endpoint')})",
        f"- Universe: {', '.join(run.strategy.universe)}",
        "",
        "## Missing data",
        "",
        "| Symbol | Missing intervals in test window |",
        "|---|---|",
    ]
    for sym, cov in manifest.get("coverage", {}).items():
        gaps = cov["test_window_missing_intervals"]
        txt = "none" if not gaps else "; ".join(f"{a} → {b}" for a, b in gaps)
        lines.append(f"| {sym} | {txt} |")
    lines += ["", "## Retrieval errors", ""]
    for e in manifest.get("errors", []):
        lines.append(f"- `{e['symbol']}` {e['kind']}: {e['error']}")
    lines += ["", "## Metrics", "",
              "All metrics (starting/ending equity, P&L, win rate, trades, profit factor, "
              "drawdown, Sharpe, Sortino, fees, funding, slippage, liquidations, long/short and "
              "per-symbol performance) are **not available** because no simulation ran.",
              "LEVERAGE_COMPARISON.csv, TRADE_LEDGER.csv and EQUITY_CURVE.csv contain headers only.",
              "", "## How to complete the run", "",
              "Run on a machine whose network can reach the exchange API:",
              "", "```", "python run_48h_backtest.py            # Binance (primary)",
              "python run_48h_backtest.py --exchange bybit --reports reports/bybit",
              "```", "",
              "Or download the CSVs elsewhere into `data/historical/<exchange>/` and run "
              "`python run_48h_backtest.py --offline`."]
    (reports / "BACKTEST_48H.md").write_text("\n".join(lines) + "\n")


def executed_report(reports: Path, run, rows, comp: pd.DataFrame, wf: dict, manifest: dict,
                    chart_paths, liq):
    best = comp[comp.scenario == "normal"].sort_values("ending_equity", ascending=False).iloc[0]
    prim = [r for r in rows if r["name"] == "A_1x_equity"][0]
    L = [f"# 48-Hour Backtest — Crypto Momentum + Funding",
         "", f"Generated: {datetime.now(timezone.utc).isoformat()}", "",
         "**Status: EXECUTED on downloaded historical data** (simulation only; live trading disabled).",
         "", f"- Test window: `{run.start}` → `{run.end}` (exclusive), {run.strategy.bar_interval} bars",
         f"- Exchange: `{manifest.get('exchange')}` — {manifest.get('endpoint')}",
         f"- Universe: {', '.join(run.strategy.universe)}",
         f"- Contract specs: {manifest.get('contract_specs_source')}",
         "- Bid/ask spread: no historical quote data retrieved; modelled with per-symbol "
         "assumed half-spread (see STRATEGY_CONFIGURATION.json)",
         f"- Starting capital per experiment: ${run.initial_capital:,.2f}; target ${run.target_equity:,.2f}",
         "", "## Leverage comparison", "",
         "| Run | Lev | Sizing | Scenario | End equity | Net P&L | Return % | Trades | Win % | PF | Max DD % | Sharpe | Fees | Funding net | Slippage | Liquidated | P(liq) model | ≥ $1,100 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for _, r in comp.iterrows():
        L.append(f"| {r['name']} | {int(r.leverage)}× | {r.sizing} | {r.scenario} | "
                 f"{_f(r.ending_equity)} | {_f(r.net_profit)} | {_f(r.return_pct)} | {r.num_trades} | "
                 f"{_f(r.win_rate_pct)} | {_f(r.profit_factor)} | {_f(r.max_drawdown_pct)} | "
                 f"{_f(r.sharpe_annualised)} | {_f(r.total_fees)} | {_f(r.total_funding_net)} | "
                 f"{_f(r.total_slippage)} | {R._fmt(bool(r.liquidations))} | "
                 f"{_f(r.liquidation_probability_model, 3)} | {R._fmt(bool(r.reached_target_1100))} |")
    L += ["", f"Best ending equity (normal costs): **{best['name']}** at ${best.ending_equity:,.2f}.",
          f"Any experiment reached $1,100: **{'YES' if comp.reached_target_1100.any() else 'NO'}**.",
          "", "## Per-market contribution (equity sizing)", ""]
    for r in rows:
        if r["scenario"] == "normal" and r["sizing"] == "equity":
            ps = M.per_symbol(r["result"])
            L.append(f"**{r['name']}**")
            L.append("")
            L.append(ps.round(4).to_markdown() if not ps.empty else "_no trades_")
            L.append("")
    m = prim["metrics"]
    L += ["## Detailed metrics — 1× equity-sized run", ""]
    L += [f"- {k}: {R._fmt(v)}" for k, v in m.items()]
    L += ["", "## Experimental mode (walk-forward)", "",
          f"- Train {X.TRAIN_WINDOW}, validation {X.VALID_WINDOW}, 1× leverage",
          f"- Train-optimised candidate: `{wf['candidate']}`",
          f"- Evaluator decision: {'ACCEPTED' if wf['review']['accepted'] else 'REJECTED'} "
          f"{wf['review']['reasons']}",
          f"- Parameters applied to the test window: `{wf['chosen']}`"]
    if "test_metrics" in wf:
        tm = wf["test_metrics"]
        L.append(f"- Test-window result (1×): end ${tm['ending_equity']:,.2f}, "
                 f"return {tm['return_pct']:.2f}%, trades {tm['num_trades']}")
    L += ["", "Grid (train/validation Sharpe):", "", wf["grid"].round(3).to_markdown(index=False),
          "", "## Risk events", ""]
    for r in rows:
        for e in r["result"].events:
            L.append(f"- {r['name']}: {e}")
    L += ["", "## Charts", ""] + [f"- `{p}`" for p in chart_paths]
    L += ["", "## Caveats", "",
          "- 48 hourly bars is far too short for statistically meaningful Sharpe/Sortino; "
          "annualised figures are shown only because they were requested.",
          "- Liquidation probability is a block-bootstrap model estimate from pre-test returns; "
          "the 'Liquidated' column is what actually happened in the simulation.",
          "- Spread is assumed, not observed. Contract specs are a current snapshot, not point-in-time.",
          "- Strategy parameters are not verified against the original publication."]
    (reports / "BACKTEST_48H.md").write_text("\n".join(L) + "\n")


def _f(x, nd=2):
    return R._fmt(float(x), nd) if x is not None else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", default="binance", choices=list(D.PROVIDERS))
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "historical"))
    ap.add_argument("--reports", default=str(ROOT / "reports"))
    ap.add_argument("--offline", action="store_true", help="use local CSVs only")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--start", default=None, help="test window start, ISO UTC")
    ap.add_argument("--end", default=None, help="test window end (exclusive), ISO UTC")
    args = ap.parse_args()
    assert CFG.LIVE_TRADING_ENABLED is False

    reports = Path(args.reports); reports.mkdir(parents=True, exist_ok=True)
    symbols = args.symbols.split(",")
    run = CFG.RunConfig(exchange=args.exchange,
                        start=args.start or CFG.BACKTEST_START,
                        end=args.end or CFG.BACKTEST_END)
    run.strategy.universe = symbols
    write_config(run, reports, {"status": "pending"})

    if args.offline:
        kl, fu, specs_raw, files = D.load_local(args.exchange, symbols, "1h", Path(args.data_dir))
        manifest = {"exchange": args.exchange, "endpoint": "local files", "files": files,
                    "errors": [], "coverage": {}, "loaded_utc": datetime.now(timezone.utc).isoformat(),
                    "contract_specs_source": "local contract_specs.json" if specs_raw else
                    "tradinggym.config.DEFAULT_SPECS (documented assumptions)"}
        ok = True
        for s in symbols:
            ts = kl[s]["timestamp"] if s in kl else []
            gaps = D.missing_intervals(ts, D.to_ms(run.start), D.to_ms(run.end), D.INTERVAL_MS["1h"])
            manifest["coverage"][s] = {"test_window_missing_intervals": gaps, "complete": not gaps}
            ok &= not gaps
            if s not in fu:
                manifest["errors"].append({"symbol": s, "kind": "funding", "error": "file missing"})
                ok = False
    else:
        res = D.download(args.exchange, symbols, "1h", X.FETCH_START, run.end, run.start, run.end,
                         Path(args.data_dir))
        kl, fu, specs_raw, manifest, ok = res.klines, res.funding, res.specs, res.manifest, res.ok
    manifest["live_trading_enabled"] = False
    (reports / "DATA_MANIFEST.json").write_text(json.dumps(manifest, indent=2, default=str))

    if not ok:
        write_config(run, reports, {"status": "NOT_EXECUTED_DATA_UNAVAILABLE"})
        not_executed_report(reports, manifest, run)
        print("DATA UNAVAILABLE — backtest not executed. See reports/BACKTEST_48H.md")
        return 2

    specs = X.build_specs(specs_raw, symbols)
    log_dir = reports / "agent_logs"
    rows = X.leverage_grid(run, kl, fu, specs, log_dir)
    liq = X.liquidation_probabilities(rows, kl, specs, run.start)
    wf = X.walk_forward(run, kl, fu, specs, log_dir)
    import copy
    from dataclasses import replace
    r1 = copy.deepcopy(run)
    r1.risk = replace(r1.risk, max_leverage=1.0)
    r1.strategy = replace(r1.strategy, mode="experimental",
                          momentum_lookback_hours=wf["chosen"]["lookback_h"],
                          momentum_weight=wf["chosen"]["mom_w"],
                          funding_weight=round(1 - wf["chosen"]["mom_w"], 2),
                          rebalance_drift=wf["chosen"]["drift"])
    wf["test_metrics"] = M.summarize(X.run_one(r1, kl, fu, specs))
    comp = R.write_tables(rows, liq, reports)
    paths = R.charts(rows, reports)
    write_config(run, reports, {"status": "EXECUTED",
                                "experimental_mode_chosen_params": wf["chosen"]})
    executed_report(reports, run, rows, comp, wf, manifest, paths, liq)
    print(comp[["name", "ending_equity", "return_pct", "num_trades", "max_drawdown_pct",
                "liquidations"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
