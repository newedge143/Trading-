"""
Unit + integration tests for the tradinggym backtester.

All price series below are SYNTHETIC TEST FIXTURES used only to verify the
mechanics of the code.  They are never written to reports/ and never used as
backtest results.
"""

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradinggym import costs as C
from tradinggym import metrics as M
from tradinggym import strategy as S
from tradinggym.agents import DecisionLog, RiskManagerAgent, StrategyEvaluator
from tradinggym.config import (ContractSpec, CostConfig, RiskConfig, RunConfig,
                               StrategyConfig)
from tradinggym.data import align_panel, missing_intervals, to_ms
from tradinggym.engine import Backtester, Order, OrderBook, Position

ROOT = Path(__file__).resolve().parents[1]
H = 3_600_000
SPEC = ContractSpec("TEST", 0.01, 0.001, 0.001, 5.0, 0.005, half_spread_bps=0.0, slippage_bps=0.0)


# ── fixtures ────────────────────────────────────────────────────────────────

def make_klines(path_fn, start="2026-10-01", hours=24 * 34, vol=1e6):
    t0 = to_ms(start)
    rows = []
    for k in range(hours):
        o = path_fn(k)
        c = path_fn(k + 1)
        rows.append([t0 + k * H, o, max(o, c) * 1.001, min(o, c) * 0.999, c, vol, vol * c])
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume",
                                       "quote_volume"])


def make_funding(rate, start="2026-10-01", hours=24 * 34):
    t0 = to_ms(start)
    return pd.DataFrame({"timestamp": [t0 + k * H for k in range(0, hours, 8)],
                         "funding_rate": rate, "mark_price": np.nan})


def three_asset(trend=(0.001, 0.0, -0.001)):
    syms = ["AUSDT", "BUSDT", "CUSDT"]
    kl = {s: make_klines(lambda k, g=g: 100 * (1 + g) ** k) for s, g in zip(syms, trend)}
    fu = {s: make_funding(0.0001) for s in syms}
    return syms, kl, fu


def run_cfg(syms, lev=1.0, sizing="equity", **risk):
    r = RunConfig(start="2026-10-20T00:00:00Z", end="2026-10-22T00:00:00Z")
    r.strategy.universe = syms
    r.risk = RiskConfig(max_leverage=lev, sizing=sizing, min_quote_volume_24h=0, **risk)
    return r


# ── fees / costs ────────────────────────────────────────────────────────────

def test_taker_fee():
    assert C.fee(1000, CostConfig()) == pytest.approx(0.5)


def test_maker_fee_and_multiplier():
    c = CostConfig(use_maker=True, fee_multiplier=2.0)
    assert C.fee(-1000, c) == pytest.approx(0.4)


def test_fill_price_spread_and_slippage_direction():
    spec = ContractSpec("X", 0.01, 0.001, 0.001, 5, 0.005, half_spread_bps=1, slippage_bps=2)
    buy, hs, sl = C.fill_price(100.0, 1, spec, CostConfig())
    sell, _, _ = C.fill_price(100.0, -1, spec, CostConfig())
    assert buy == pytest.approx(100.03) and sell == pytest.approx(99.97)
    assert hs == pytest.approx(0.01) and sl == pytest.approx(0.02)
    b2, _, _ = C.fill_price(100.0, 1, spec, CostConfig(slippage_multiplier=2, spread_multiplier=2))
    assert b2 == pytest.approx(100.06)


# ── funding ─────────────────────────────────────────────────────────────────

def test_funding_long_pays_positive_rate():
    assert C.funding_payment(2.0, 100.0, 0.0001, CostConfig()) == pytest.approx(-0.02)


def test_funding_short_receives_positive_rate():
    assert C.funding_payment(-2.0, 100.0, 0.0001, CostConfig()) == pytest.approx(0.02)


def test_funding_adverse_stress_always_costs():
    c = CostConfig(adverse_funding=True)
    assert C.funding_payment(-2.0, 100.0, 0.0001, c) == pytest.approx(-0.02)
    assert C.funding_payment(2.0, 100.0, -0.0001, c) == pytest.approx(-0.02)


def test_engine_funding_applied_only_at_funding_timestamps():
    syms = ["AUSDT"]
    kl = {"AUSDT": make_klines(lambda k: 100.0 * (1.001 ** k))}
    fu = {"AUSDT": make_funding(0.0005)}
    r = run_cfg(syms)
    res = Backtester(r, kl, fu, {"AUSDT": SPEC}).run_backtest()
    times = {pd.Timestamp(f["time"]).hour for f in res.funding_flows}
    assert times <= {0, 8, 16}
    assert res.funding_flows, "position should have been charged funding"
    for f in res.funding_flows:  # long pays positive funding
        assert f["flow"] < 0 and f["side"] == "long"


# ── position sizing / leverage ──────────────────────────────────────────────

def test_gross_notional_equity_vs_fixed():
    ra = RiskManagerAgent(RiskConfig(max_leverage=5, sizing="equity"), 100)
    rf = RiskManagerAgent(RiskConfig(max_leverage=5, sizing="fixed_notional"), 100)
    assert ra.gross_notional(150) == pytest.approx(750)
    assert rf.gross_notional(150) == pytest.approx(500)
    # fixed notional can never exceed max leverage on current equity
    assert rf.gross_notional(50) == pytest.approx(250)


def test_leverage_cap_enforced():
    with pytest.raises(ValueError):
        RiskConfig(max_leverage=20)


def test_engine_leverage_never_exceeds_cap():
    syms, kl, fu = three_asset()
    for lev in (1, 2, 5, 10):
        res = Backtester(run_cfg(syms, lev), kl, fu).run_backtest()
        used = res.equity["leverage_used"].dropna()
        # small tolerance for intrabar price movement after the fill
        assert used.max() <= lev * 1.05


def test_round_qty_down_to_step():
    assert C.round_qty(1.23456, SPEC) == pytest.approx(1.234)
    assert C.round_qty(-1.23456, SPEC) == pytest.approx(-1.234)


# ── P&L accounting ──────────────────────────────────────────────────────────

def _bt():
    syms, kl, fu = three_asset()
    bt = Backtester(run_cfg(syms), kl, fu, {s: SPEC for s in syms})
    bt.wallet, bt.fills, bt.trades, bt.trade_map = 100.0, [], [], {}
    import itertools
    bt._trade_ids = itertools.count(1)
    bt.costs = CostConfig(taker_fee=0.0)
    return bt


def test_long_round_trip_pnl():
    bt = _bt()
    p = Position("AUSDT")
    t = pd.Timestamp("2026-10-20", tz="UTC")
    bt._fill(p, Order(1, "AUSDT", 1, t, "open"), 1.0, 100.0, t, False)
    bt._fill(p, Order(2, "AUSDT", -1, t, "close"), -1.0, 110.0, t, False)
    assert bt.wallet == pytest.approx(110.0)
    assert p.qty == 0 and bt.trades[0].net_pnl == pytest.approx(10.0)


def test_short_partial_close_and_flip():
    bt = _bt()
    p = Position("AUSDT")
    t = pd.Timestamp("2026-10-20", tz="UTC")
    bt._fill(p, Order(1, "AUSDT", -2, t, "open"), -2.0, 100.0, t, False)
    bt._fill(p, Order(2, "AUSDT", 1, t, "reduce"), 1.0, 90.0, t, False)
    assert bt.wallet == pytest.approx(110.0) and p.qty == pytest.approx(-1.0)
    bt._fill(p, Order(3, "AUSDT", 3, t, "flip"), 3.0, 95.0, t, False)
    assert bt.wallet == pytest.approx(115.0)
    assert p.qty == pytest.approx(2.0) and p.entry == pytest.approx(95.0)
    assert len(bt.trades) == 2 and bt.trades[0].exit_time is not None
    assert bt.trades[0].gross_pnl == pytest.approx(15.0)


def test_fees_reduce_wallet():
    bt = _bt()
    bt.costs = CostConfig()  # 5 bps taker
    p = Position("AUSDT")
    t = pd.Timestamp("2026-10-20", tz="UTC")
    bt._fill(p, Order(1, "AUSDT", 1, t, "open"), 1.0, 100.0, t, False)
    bt._fill(p, Order(2, "AUSDT", -1, t, "close"), -1.0, 100.0, t, False)
    assert bt.wallet == pytest.approx(100.0 - 0.1)
    assert bt.trades[0].fees == pytest.approx(0.1)


def test_engine_equity_identity():
    """ending equity == start + sum(trade net pnl) for a fully closed-out run."""
    syms, kl, fu = three_asset()
    res = Backtester(run_cfg(syms, 5), kl, fu).run_backtest()
    tot = sum(t.net_pnl for t in res.trades)
    assert res.equity["equity"].iloc[-1] == pytest.approx(100 + tot, abs=1e-6)
    assert all(t.exit_time is not None for t in res.trades)


# ── liquidation ─────────────────────────────────────────────────────────────

def test_isolated_liquidation_price():
    spec = ContractSpec("X", 0.01, 0.001, 0.001, 5, 0.005)
    lp = C.liquidation_price(100.0, 1.0, 10.0, spec)  # 10x long
    assert 90.0 < lp < 91.0
    sp = C.liquidation_price(100.0, -1.0, 10.0, spec)
    assert 109.0 < sp < 110.0


def test_engine_liquidates_on_crash():
    syms = ["AUSDT", "BUSDT"]
    # A rallies for the momentum warm-up, then crashes 30 % in one bar inside the window
    crash_k = 24 * 19 + 5

    def a(k):
        return 100 * (1.002 ** min(k, crash_k)) * (0.7 if k > crash_k else 1.0)
    kl = {"AUSDT": make_klines(a), "BUSDT": make_klines(lambda k: 100 * 0.999 ** k)}
    fu = {s: make_funding(0.0) for s in syms}
    r = run_cfg(syms, 10, stop_loss_pct=0.99)  # disable stop so liquidation is reachable
    res = Backtester(r, kl, fu).run_backtest()
    assert res.liquidated
    assert res.equity["equity"].min() >= 0


def test_stop_loss_prevents_liquidation():
    syms = ["AUSDT", "BUSDT"]
    crash_k = 24 * 19 + 5

    def a(k):
        if k <= crash_k:
            return 100 * (1.002 ** k)
        return 100 * (1.002 ** crash_k) * (0.97 ** min(k - crash_k, 10))
    kl = {"AUSDT": make_klines(a), "BUSDT": make_klines(lambda k: 100 * 0.999 ** k)}
    fu = {s: make_funding(0.0) for s in syms}
    res = Backtester(run_cfg(syms, 2, stop_loss_pct=0.03), kl, fu).run_backtest()
    assert any(e["event"] == "stop_loss" for e in res.events)
    assert not res.liquidated


# ── signal generation ───────────────────────────────────────────────────────

def test_momentum_value():
    closes = pd.DataFrame({"A": [100.0, 105, 110], "B": [100.0, 95, 90]})
    m = S.momentum(closes, 2)
    assert m["A"] == pytest.approx(0.10) and m["B"] == pytest.approx(-0.10)


def test_signal_long_winner_short_loser():
    syms, kl, fu = three_asset()
    panel = align_panel(kl)
    closes = pd.DataFrame({s: panel[s]["close"] for s in syms}).iloc[: 24 * 20]
    t = closes.index[-1]
    out = S.target_weights(closes, fu, t, StrategyConfig(universe=syms))
    w = out["weights"]
    assert w["AUSDT"] > 0 and w["CUSDT"] < 0
    assert w.abs().sum() == pytest.approx(1.0)


def test_funding_component_tilts_toward_receiving_side():
    cfg = StrategyConfig(momentum_weight=0.0, funding_weight=1.0)
    mom = pd.Series({"A": 0.0, "B": 0.0, "C": 0.0})
    carry = -pd.Series({"A": 0.001, "B": 0.0, "C": -0.001})  # A crowded long
    w = S.scores_to_weights(S.combine_scores(mom + 1e-9 * np.arange(3), carry, cfg), cfg)
    assert w["A"] < 0 and w["C"] > 0


def test_weight_drift():
    assert S.weight_drift(pd.Series({"A": 0.5, "B": -0.5}), pd.Series({"A": 0.5, "B": -0.5})) == 0
    assert S.weight_drift(pd.Series({"A": 0.4, "B": -0.5}),
                          pd.Series({"A": 0.5, "B": -0.5})) == pytest.approx(0.1)


# ── data alignment & no look-ahead ──────────────────────────────────────────

def test_missing_intervals_detected():
    t0 = to_ms("2026-11-02T00:00:00Z")
    have = [t0 + k * H for k in range(48) if not 10 <= k < 13]
    gaps = missing_intervals(have, t0, t0 + 48 * H, H)
    assert gaps == [("2026-11-02T10:00:00Z", "2026-11-02T13:00:00Z")]
    assert missing_intervals([], t0, t0 + 2 * H, H) == [("2026-11-02T00:00:00Z",
                                                         "2026-11-02T02:00:00Z")]


def test_align_panel_keeps_gaps_as_nan():
    a = make_klines(lambda k: 100.0, hours=5)
    b = a.drop(index=2).reset_index(drop=True)
    p = align_panel({"A": a, "B": b})
    assert len(p["B"]) == 5 and p["B"]["close"].isna().sum() == 1


def test_assert_no_future_raises():
    s = pd.Series([1, 2], index=pd.to_datetime(["2026-11-02", "2026-11-03"], utc=True))
    with pytest.raises(AssertionError):
        S.assert_no_future(s, pd.Timestamp("2026-11-02", tz="UTC"))


def test_trailing_funding_excludes_future():
    df = pd.DataFrame({"timestamp": [0, 10, 20], "funding_rate": [1.0, 2.0, 99.0]})
    assert S.trailing_funding({"A": df}, 15, 5)["A"] == pytest.approx(1.5)


def test_no_lookahead_future_shock_does_not_change_past_decisions():
    """Altering prices AFTER bar k must not change any decision made at <= k."""
    syms, kl, fu = three_asset()
    base = Backtester(run_cfg(syms, 2), kl, fu).run_backtest()
    kl2 = {s: df.copy() for s, df in kl.items()}
    cut = to_ms("2026-10-21T00:00:00Z")
    for s in syms:
        m = kl2[s]["timestamp"] >= cut
        kl2[s].loc[m, ["open", "high", "low", "close"]] *= np.where(s == "AUSDT", 0.5, 2.0)
    shock = Backtester(run_cfg(syms, 2), kl2, fu).run_backtest()
    before = lambda res: [(f.time, f.symbol, round(f.qty, 9), round(f.price, 9))
                          for f in res.fills if f.time < pd.Timestamp(cut, unit="ms", tz="UTC")]
    assert before(base) == before(shock)
    wb = [w for w in base.weights_history if pd.Timestamp(w["time"]) <= pd.Timestamp(cut, unit="ms", tz="UTC")]
    ws = [w for w in shock.weights_history if pd.Timestamp(w["time"]) <= pd.Timestamp(cut, unit="ms", tz="UTC")]
    assert wb == ws


def test_orders_fill_at_next_open_not_signal_close():
    syms, kl, fu = three_asset()
    res = Backtester(run_cfg(syms), kl, fu).run_backtest()
    panel = align_panel(kl)
    first = [f for f in res.fills if f.reason == "rebalance"][0]
    assert first.reference_price == pytest.approx(panel[first.symbol].loc[first.time, "open"])
    assert first.time > pd.Timestamp("2026-10-20T00:00:00Z")  # decision at close of bar 0


# ── drawdown & metrics ──────────────────────────────────────────────────────

def test_max_drawdown():
    eq = pd.Series([100, 120, 90, 130, 65])
    assert M.max_drawdown(eq) == pytest.approx(0.5)


def test_profit_factor_and_sharpe_signs():
    assert M.profit_factor([10, -5, 5]) == pytest.approx(3.0)
    assert math.isinf(M.profit_factor([1, 2]))
    assert M.sharpe(pd.Series([0.01, 0.02, 0.015])) > 0


def test_daily_loss_halt_and_drawdown_shutdown_trigger():
    syms = ["AUSDT", "BUSDT"]
    k0 = 24 * 19 + 2

    def a(k):  # trend up, then grind down inside the test window
        return 100 * (1.002 ** min(k, k0)) * (0.99 ** max(0, k - k0))
    kl = {"AUSDT": make_klines(a), "BUSDT": make_klines(lambda k: 100 * 0.9995 ** k)}
    fu = {s: make_funding(0.0) for s in syms}
    r = run_cfg(syms, 10, stop_loss_pct=0.99, daily_loss_limit=0.10, max_drawdown_limit=0.30)
    res = Backtester(r, kl, fu).run_backtest()
    kinds = {e["event"] for e in res.events}
    assert kinds & {"DAILY_LOSS_HALT", "EMERGENCY_SHUTDOWN", "LIQUIDATION"}


# ── duplicate order prevention ──────────────────────────────────────────────

def test_duplicate_orders_rejected():
    ob = OrderBook()
    t = pd.Timestamp("2026-11-02", tz="UTC")
    assert ob.submit("A", 1.0, t, "x") is not None
    assert ob.submit("A", 2.0, t, "x") is None
    assert ob.rejected_duplicates == 1
    o2 = ob.submit("B", 1.0, t, "x")
    ids = [o.id for o in ob.pop_all()]
    assert len(set(ids)) == 2 and not ob.pending
    assert ob.submit("A", 1.0, t, "x").id not in ids


def test_engine_never_holds_two_positions_per_symbol():
    syms, kl, fu = three_asset()
    res = Backtester(run_cfg(syms, 5), kl, fu).run_backtest()
    for s in syms:
        open_ = [t for t in res.trades if t.symbol == s]
        ivs = sorted((t.entry_time, t.exit_time) for t in open_)
        for (a0, a1), (b0, b1) in zip(ivs, ivs[1:]):
            assert b0 >= a1  # no overlap


# ── agents / evaluator ──────────────────────────────────────────────────────

def test_evaluator_rejects_overfit():
    ev = StrategyEvaluator(DecisionLog())
    out = ev.accept_change({"train_sharpe": 1.0, "valid_sharpe": 0.8, "valid_return": 1},
                           {"train_sharpe": 5.0, "valid_sharpe": 0.9, "valid_return": 1})
    assert not out["accepted"]
    ok = ev.accept_change({"train_sharpe": 1.0, "valid_sharpe": 0.5, "valid_return": 1},
                          {"train_sharpe": 1.5, "valid_sharpe": 1.2, "valid_return": 2})
    assert ok["accepted"]


def test_decision_log_is_structured_jsonl(tmp_path):
    p = tmp_path / "log.jsonl"
    syms, kl, fu = three_asset()
    Backtester(run_cfg(syms), kl, fu, log_path=p).run_backtest()
    entries = [json.loads(l) for l in p.read_text().splitlines()]
    agents = {e["agent"] for e in entries}
    assert {"market_scanner", "momentum_analyst", "funding_analyst", "risk_manager"} <= agents


# ── end-to-end pipeline (offline, synthetic fixture into a temp dir) ────────

def test_pipeline_end_to_end_offline(tmp_path):
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    d = tmp_path / "data" / "binance"
    d.mkdir(parents=True)
    for s, g in zip(syms, (0.0004, 0.0, -0.0003)):
        make_klines(lambda k, g=g: 100 * (1 + g) ** k * (1 + 0.003 * math.sin(k / 3)),
                    start="2026-08-25", hours=24 * 71, vol=1e7).to_csv(d / f"{s}_1h_klines.csv", index=False)
        make_funding(0.0001, start="2026-08-25", hours=24 * 71).to_csv(d / f"{s}_funding.csv", index=False)
    rep = tmp_path / "reports"
    r = subprocess.run([sys.executable, str(ROOT / "run_48h_backtest.py"), "--offline",
                        "--data-dir", str(tmp_path / "data"), "--reports", str(rep)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    for f in ["BACKTEST_48H.md", "LEVERAGE_COMPARISON.csv", "TRADE_LEDGER.csv", "EQUITY_CURVE.csv",
              "DATA_MANIFEST.json", "STRATEGY_CONFIGURATION.json"]:
        assert (rep / f).exists(), f
    comp = pd.read_csv(rep / "LEVERAGE_COMPARISON.csv")
    assert len(comp) == 12 and set(comp.leverage) == {1, 2, 5, 10}
    assert (comp.starting_equity == 100).all()
    assert len(list((rep / "charts").glob("*.png"))) == 5


def test_pipeline_reports_missing_data(tmp_path):
    rep = tmp_path / "reports"
    r = subprocess.run([sys.executable, str(ROOT / "run_48h_backtest.py"), "--offline",
                        "--data-dir", str(tmp_path / "nodata"), "--reports", str(rep)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2
    md = (rep / "BACKTEST_48H.md").read_text()
    assert "NOT EXECUTED" in md and "2026-11-02T00:00:00Z → 2026-11-04T00:00:00Z" in md
    assert len(pd.read_csv(rep / "TRADE_LEDGER.csv")) == 0
