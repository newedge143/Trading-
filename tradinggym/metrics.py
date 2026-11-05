"""Performance metrics computed from a BacktestResult."""

from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd

from tradinggym.engine import BacktestResult

HOURS_PER_YEAR = 24 * 365


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = np.where(peak > 0, 1 - equity / peak, 0.0)
    return float(np.max(dd))


def sharpe(returns: pd.Series, periods_per_year: float = HOURS_PER_YEAR) -> float:
    r = returns.dropna()
    if len(r) < 2 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * np.sqrt(periods_per_year))


def sortino(returns: pd.Series, periods_per_year: float = HOURS_PER_YEAR) -> float:
    r = returns.dropna()
    if len(r) < 2:
        return float("nan")
    downside = np.sqrt(np.mean(np.minimum(r, 0.0) ** 2))
    if downside == 0:
        return float("nan")
    return float(r.mean() / downside * np.sqrt(periods_per_year))


def profit_factor(pnls: List[float]) -> float:
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return gains / losses


def summarize(res: BacktestResult) -> Dict:
    eq = res.equity["equity"]
    start, end = float(res.initial_capital), float(eq.iloc[-1])
    closed = [t for t in res.trades if t.exit_time is not None]
    pnls = [t.net_pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rets = eq.pct_change().replace([np.inf, -np.inf], np.nan)
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    out = {
        "starting_equity": start,
        "ending_equity": end,
        "net_profit": end - start,
        "return_pct": (end / start - 1) * 100 if start else float("nan"),
        "num_trades": len(closed),
        "num_fills": len(res.fills),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": 100 * len(wins) / len(closed) if closed else float("nan"),
        "profit_factor": profit_factor(pnls),
        "max_drawdown_pct": 100 * max_drawdown(eq),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_reward_risk": abs(avg_win / avg_loss) if avg_loss else float("nan"),
        "sharpe_annualised": sharpe(rets),
        "sortino_annualised": sortino(rets),
        "total_fees": float(sum(f.fee for f in res.fills)),
        "total_funding_net": float(sum(x["flow"] for x in res.funding_flows)),
        "funding_paid": float(-sum(x["flow"] for x in res.funding_flows if x["flow"] < 0)),
        "funding_received": float(sum(x["flow"] for x in res.funding_flows if x["flow"] > 0)),
        "total_spread_cost": float(sum(f.spread_cost for f in res.fills)),
        "total_slippage": float(sum(f.slippage_cost for f in res.fills)),
        "liquidations": int(sum(1 for e in res.events if e["event"] == "LIQUIDATION")),
        "liquidated": bool(res.liquidated),
        "halted": res.halted,
        "max_leverage_used": float(res.equity["leverage_used"].max(skipna=True) or 0.0),
        "rejected_orders": len(res.rejected_orders),
        "reached_target_1100": end >= 1100.0,
    }
    for side in ("long", "short"):
        ts = [t for t in closed if t.side == side]
        out[f"{side}_trades"] = len(ts)
        out[f"{side}_net_pnl"] = float(sum(t.net_pnl for t in ts))
    return out


def per_symbol(res: BacktestResult) -> pd.DataFrame:
    rows = {}
    for t in res.trades:
        r = rows.setdefault(t.symbol, {"trades": 0, "gross_pnl": 0.0, "fees": 0.0,
                                       "funding": 0.0, "slippage": 0.0, "spread": 0.0,
                                       "net_pnl": 0.0, "wins": 0})
        r["trades"] += 1
        r["gross_pnl"] += t.gross_pnl
        r["fees"] += t.fees
        r["funding"] += t.funding
        r["slippage"] += t.slippage_cost
        r["spread"] += t.spread_cost
        r["net_pnl"] += t.net_pnl
        r["wins"] += int(t.net_pnl > 0)
    return pd.DataFrame.from_dict(rows, orient="index")


def liquidation_probability(returns: pd.DataFrame, weights: Dict[str, float], leverage: float,
                            mmr: Dict[str, float], horizon: int = 48, n_sims: int = 5000,
                            block: int = 6, seed: int = 7) -> float:
    """Block-bootstrap estimate of P(liquidation within `horizon` bars).

    `returns`: historical simple returns per symbol (bars known BEFORE the test
    window).  Uses intrabar close-to-close paths only, so it understates wick
    risk; it is a model estimate, not an observation.
    """
    r = returns[list(weights)].dropna()
    if len(r) < block * 2 or leverage <= 0:
        return float("nan")
    rng = np.random.default_rng(seed)
    w = np.array([weights[s] for s in r.columns]) * leverage
    m = np.array([mmr[s] for s in r.columns])
    arr = r.to_numpy()
    n_blocks = int(np.ceil(horizon / block))
    hits = 0
    for _ in range(n_sims):
        starts = rng.integers(0, len(arr) - block, n_blocks)
        path = np.concatenate([arr[s:s + block] for s in starts])[:horizon]
        prices = np.cumprod(1 + path, axis=0)          # relative to entry
        pnl = (prices - 1) @ w                           # per unit of equity
        equity = 1 + pnl
        maint = (np.abs(w) * prices) @ m
        if np.any(equity <= maint):
            hits += 1
    return hits / n_sims
