"""Report and chart writers.  Every number written here comes from a
BacktestResult produced by the engine on downloaded data."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from tradinggym import metrics as M

# Reference categorical palette, fixed order (dataviz skill): blue, orange, aqua, yellow
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, INK2, SURFACE, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#e4e3df"
GOOD, CRITICAL = "#0ca30c", "#d03b3b"

LEDGER_COLS = ["experiment", "trade_id", "symbol", "side", "entry_time", "entry_price",
               "exit_time", "exit_price", "max_qty", "entry_notional", "gross_pnl", "fees",
               "funding", "spread_cost", "slippage_cost", "net_pnl", "exit_reason"]
EQUITY_COLS = ["experiment", "timestamp", "equity", "wallet", "unrealized", "gross_exposure",
               "leverage_used", "drawdown"]
COMPARISON_COLS = ["name", "experiment", "leverage", "sizing", "scenario", "starting_equity",
                   "ending_equity", "net_profit", "return_pct", "num_trades", "winning_trades",
                   "losing_trades", "win_rate_pct", "profit_factor", "max_drawdown_pct",
                   "avg_win", "avg_loss", "avg_reward_risk", "sharpe_annualised",
                   "sortino_annualised", "total_fees", "total_funding_net", "funding_paid",
                   "funding_received", "total_spread_cost", "total_slippage", "liquidations",
                   "liquidation_probability_model", "long_trades", "long_net_pnl",
                   "short_trades", "short_net_pnl", "max_leverage_used", "rejected_orders",
                   "halted", "reached_target_1100", "status"]


def _fmt(x, nd=2):
    if x is None:
        return "n/a"
    if isinstance(x, (bool, np.bool_)):
        return "yes" if x else "no"
    if isinstance(x, (float, np.floating)):
        if math.isnan(x):
            return "n/a"
        if math.isinf(x):
            return "∞"
        return f"{x:,.{nd}f}"
    return str(x)


def write_empty_outputs(reports: Path):
    """Header-only CSVs for a run that could not execute (no fabricated rows)."""
    pd.DataFrame(columns=COMPARISON_COLS).to_csv(reports / "LEVERAGE_COMPARISON.csv", index=False)
    pd.DataFrame(columns=LEDGER_COLS).to_csv(reports / "TRADE_LEDGER.csv", index=False)
    pd.DataFrame(columns=EQUITY_COLS).to_csv(reports / "EQUITY_CURVE.csv", index=False)


def write_tables(rows: List[dict], liq_prob: Dict[str, float], reports: Path) -> pd.DataFrame:
    comp, ledger, curves = [], [], []
    for r in rows:
        m = dict(r["metrics"])
        m.update({"name": r["name"], "experiment": r["experiment"], "leverage": r["leverage"],
                  "sizing": r["sizing"], "scenario": r["scenario"], "status": "EXECUTED",
                  "liquidation_probability_model": liq_prob.get(r["name"], float("nan"))})
        comp.append(m)
        for t in r["result"].trades:
            ledger.append({"experiment": r["name"], "trade_id": t.trade_id, "symbol": t.symbol,
                           "side": t.side, "entry_time": t.entry_time, "entry_price": t.entry_price,
                           "exit_time": t.exit_time, "exit_price": t.exit_price,
                           "max_qty": t.max_qty, "entry_notional": t.entry_notional,
                           "gross_pnl": t.gross_pnl, "fees": t.fees, "funding": t.funding,
                           "spread_cost": t.spread_cost, "slippage_cost": t.slippage_cost,
                           "net_pnl": t.net_pnl, "exit_reason": t.exit_reason})
        eq = r["result"].equity.reset_index()
        eq.insert(0, "experiment", r["name"])
        curves.append(eq)
    df = pd.DataFrame(comp)[COMPARISON_COLS]
    df.to_csv(reports / "LEVERAGE_COMPARISON.csv", index=False)
    pd.DataFrame(ledger, columns=LEDGER_COLS).to_csv(reports / "TRADE_LEDGER.csv", index=False)
    pd.concat(curves)[EQUITY_COLS].to_csv(reports / "EQUITY_CURVE.csv", index=False)
    return df


def _style(ax, title):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    ax.grid(True, color=GRID, linewidth=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(INK2)
    ax.tick_params(colors=INK2, labelsize=8)


def charts(rows: List[dict], reports: Path) -> List[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out_dir = reports / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    normal_eq = [r for r in rows if r["scenario"] == "normal" and r["sizing"] == "equity"]

    def save(fig, name):
        p = out_dir / name
        fig.patch.set_facecolor(SURFACE)
        fig.tight_layout()
        fig.savefig(p, dpi=150)
        plt.close(fig)
        paths.append(str(p))

    # 1. equity curves
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for c, r in zip(SERIES, normal_eq):
        e = r["result"].equity["equity"]
        ax.plot(e.index, e.values, color=c, linewidth=2, label=f"{int(r['leverage'])}×")
    ax.axhline(100, color=INK2, linewidth=0.8, linestyle="--")
    ax.set_ylabel("Equity (USD)", color=INK2)
    _style(ax, "Account equity, equity-based sizing (start $100)")
    ax.legend(frameon=False, fontsize=8, title="Max leverage", title_fontsize=8)
    save(fig, "equity_curve.png")

    # 2. drawdown
    fig, ax = plt.subplots(figsize=(9, 4))
    for c, r in zip(SERIES, normal_eq):
        d = r["result"].equity["drawdown"] * 100
        ax.plot(d.index, -d.values, color=c, linewidth=2, label=f"{int(r['leverage'])}×")
    ax.set_ylabel("Drawdown (%)", color=INK2)
    _style(ax, "Drawdown from running peak, equity-based sizing")
    ax.legend(frameon=False, fontsize=8, title="Max leverage", title_fontsize=8)
    save(fig, "drawdown_curve.png")

    # 3. per-symbol contribution (1x and 10x equity) – grouped bars
    fig, ax = plt.subplots(figsize=(9, 4))
    syms = sorted({t.symbol for r in normal_eq for t in r["result"].trades}) or ["—"]
    x = np.arange(len(syms))
    width = 0.8 / max(len(normal_eq), 1)
    for k, (c, r) in enumerate(zip(SERIES, normal_eq)):
        ps = M.per_symbol(r["result"])
        vals = [float(ps.loc[s, "net_pnl"]) if s in ps.index else 0.0 for s in syms]
        ax.bar(x + k * width - 0.4 + width / 2, vals, width * 0.92, color=c,
               label=f"{int(r['leverage'])}×")
    ax.set_xticks(x, syms)
    ax.axhline(0, color=INK2, linewidth=0.8)
    ax.set_ylabel("Net P&L (USD)", color=INK2)
    _style(ax, "Net P&L contribution by market")
    ax.legend(frameon=False, fontsize=8, title="Max leverage", title_fontsize=8)
    save(fig, "symbol_contribution.png")

    # 4. leverage comparison: ending equity, normal vs stress (one axis)
    fig, ax = plt.subplots(figsize=(9, 4))
    groups = [("equity", "normal", "Equity sizing"), ("fixed_notional", "normal", "Fixed notional"),
              ("equity", "stress", "Equity sizing, stress")]
    levs = sorted({r["leverage"] for r in rows})
    x = np.arange(len(levs))
    w = 0.8 / len(groups)
    for k, (sz, sc, lab) in enumerate(groups):
        vals = []
        for L in levs:
            m = [r for r in rows if r["leverage"] == L and r["sizing"] == sz and r["scenario"] == sc]
            vals.append(m[0]["metrics"]["ending_equity"] if m else np.nan)
        ax.bar(x + k * w - 0.4 + w / 2, vals, w * 0.92, color=SERIES[k], label=lab)
    ax.axhline(100, color=INK2, linewidth=0.8, linestyle="--")
    ax.axhline(1100, color=GOOD, linewidth=0.8, linestyle=":")
    ax.text(len(levs) - 0.5, 1100, " $1,100 target", color=INK2, fontsize=8, va="bottom", ha="right")
    ax.set_xticks(x, [f"{int(L)}×" for L in levs])
    ax.set_ylabel("Ending equity (USD)", color=INK2)
    _style(ax, "Ending equity by leverage and scenario")
    ax.legend(frameon=False, fontsize=8)
    save(fig, "leverage_comparison.png")

    # 5. win/loss distribution (all normal runs pooled per leverage)
    fig, ax = plt.subplots(figsize=(9, 4))
    for c, r in zip(SERIES, normal_eq):
        pnl = [t.net_pnl for t in r["result"].trades if t.exit_time is not None]
        if pnl:
            ax.hist(pnl, bins=20, color=c, alpha=0.55, label=f"{int(r['leverage'])}× (n={len(pnl)})",
                    edgecolor=SURFACE, linewidth=1)
    ax.axvline(0, color=INK2, linewidth=0.8)
    ax.set_xlabel("Net P&L per round-trip trade (USD)", color=INK2)
    ax.set_ylabel("Trades", color=INK2)
    _style(ax, "Win/loss distribution, equity-based sizing")
    ax.legend(frameon=False, fontsize=8)
    save(fig, "win_loss_distribution.png")
    return paths
