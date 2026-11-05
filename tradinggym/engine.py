"""
Event-driven multi-asset perpetual-futures backtester.

Timeline per bar i (bar open T_i, close T_i + 1 bar):
  1. Funding events with T_{i-1} < ts <= T_i are settled on positions held
     at that moment (before any fill at T_i).
  2. Pending market orders (decided at the close of bar i-1) fill at the OPEN
     of bar i, plus half-spread and slippage, capped at a participation rate
     of the PREVIOUS bar's volume (partial fills; remainder cancelled).
  3. Intrabar: stop-losses (gap-aware), then cross-margin liquidation check at
     every position's adverse extreme (low for longs, high for shorts).
  4. Mark-to-market at the close; daily-loss / drawdown controls.
  5. Agents see data up to and including bar i (all <= T_{i+1}) and may
     submit orders for bar i+1.  No row after bar i is ever passed to them.
At the end of the window all positions are closed at the final close price
(with full costs) so results are fully realised.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from tradinggym import costs as C
from tradinggym.agents import (DecisionLog, FundingAnalyst, MarketScanner,
                               MomentumAnalyst, RiskManagerAgent)
from tradinggym.config import ContractSpec, RunConfig, default_spec
from tradinggym.data import INTERVAL_MS
from tradinggym import strategy as strat


@dataclass
class Order:
    id: int
    symbol: str
    qty: float           # signed
    created: pd.Timestamp
    reason: str
    reduce_only: bool = False


@dataclass
class Fill:
    order_id: int
    time: pd.Timestamp
    symbol: str
    side: str
    qty: float
    reference_price: float
    price: float
    notional: float
    fee: float
    spread_cost: float
    slippage_cost: float
    reason: str
    partial: bool


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    entry: float = 0.0
    trade_id: Optional[int] = None


@dataclass
class TradeRecord:
    trade_id: int
    symbol: str
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: float = 0.0
    max_qty: float = 0.0
    entry_notional: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    exit_reason: str = ""
    _exit_qty: float = 0.0
    _exit_value: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees + self.funding


class OrderBook:
    """At most one pending order per symbol; ids are unique and never reused."""

    def __init__(self):
        self.pending: Dict[str, Order] = {}
        self._ids = itertools.count(1)
        self.rejected_duplicates = 0

    def submit(self, symbol, qty, t, reason, reduce_only=False) -> Optional[Order]:
        if symbol in self.pending:
            self.rejected_duplicates += 1
            return None
        o = Order(next(self._ids), symbol, qty, t, reason, reduce_only)
        self.pending[symbol] = o
        return o

    def pop_all(self) -> List[Order]:
        out = list(self.pending.values())
        self.pending.clear()
        return out


@dataclass
class BacktestResult:
    config: dict
    equity: pd.DataFrame
    fills: List[Fill]
    trades: List[TradeRecord]
    events: List[dict]
    liquidated: bool
    halted: Optional[str]
    funding_flows: List[dict]
    rejected_orders: List[dict]
    initial_capital: float
    weights_history: List[dict] = field(default_factory=list)


class Backtester:
    def __init__(self, run: RunConfig, klines: Dict[str, pd.DataFrame],
                 funding: Dict[str, pd.DataFrame],
                 specs: Optional[Dict[str, ContractSpec]] = None,
                 log_path: Optional[Path] = None):
        from tradinggym.data import align_panel
        self.run = run
        self.cfg, self.risk, self.costs = run.strategy, run.risk, run.costs
        syms = [s for s in self.cfg.universe if s in klines]
        self.symbols = syms
        self.panel = align_panel({s: klines[s] for s in syms})
        self.funding = {s: (funding.get(s) if funding.get(s) is not None
                            else pd.DataFrame(columns=["timestamp", "funding_rate", "mark_price"]))
                        for s in syms}
        self.specs = {s: (specs or {}).get(s) or default_spec(s) for s in syms}
        self.step = pd.Timedelta(milliseconds=INTERVAL_MS[self.cfg.bar_interval])
        self.log = DecisionLog(log_path)
        self.scanner = MarketScanner(self.risk, bars_per_day=int(pd.Timedelta("1D") / self.step))
        self.mom_agent = MomentumAnalyst(self.cfg)
        self.fund_agent = FundingAnalyst(self.cfg, self.risk)
        self.risk_agent = RiskManagerAgent(self.risk, run.initial_capital)

    # ── accounting helpers ────────────────────────────────────────────────
    def _fill(self, pos: Position, order: Order, qty: float, ref: float,
              t: pd.Timestamp, partial: bool):
        side = 1 if qty > 0 else -1
        spec = self.specs[pos.symbol]
        price, hs, sl = C.fill_price(ref, side, spec, self.costs)
        notional = abs(qty) * price
        fee = C.fee(notional, self.costs)
        f = Fill(order.id, t, pos.symbol, "buy" if side > 0 else "sell", abs(qty), ref, price,
                 notional, fee, abs(qty) * hs, abs(qty) * sl, order.reason, partial)
        self.fills.append(f)
        self.wallet -= fee
        remaining = qty
        # reduce / close existing position first
        if pos.qty != 0 and np.sign(pos.qty) != side:
            close_qty = min(abs(pos.qty), abs(qty))
            pnl = close_qty * (price - pos.entry) * np.sign(pos.qty)
            self.wallet += pnl
            tr = self.trade_map[pos.trade_id]
            tr.gross_pnl += pnl
            frac = close_qty / abs(qty)
            tr.fees += fee * frac
            tr.spread_cost += f.spread_cost * frac
            tr.slippage_cost += f.slippage_cost * frac
            tr._exit_qty += close_qty
            tr._exit_value += close_qty * price
            pos.qty += side * close_qty
            remaining -= side * close_qty
            if abs(pos.qty) < 1e-12:
                pos.qty = 0.0
                tr.exit_time, tr.exit_price = t, tr._exit_value / tr._exit_qty
                tr.exit_reason = order.reason
                pos.trade_id, pos.entry = None, 0.0
            fee *= (1 - frac)
            f_spread, f_slip = f.spread_cost * (1 - frac), f.slippage_cost * (1 - frac)
        else:
            f_spread, f_slip = f.spread_cost, f.slippage_cost
        if abs(remaining) > 1e-12:
            if pos.qty == 0:
                tid = next(self._trade_ids)
                tr = TradeRecord(tid, pos.symbol, "long" if remaining > 0 else "short", t, price)
                self.trades.append(tr)
                self.trade_map[tid] = tr
                pos.trade_id, pos.entry, pos.qty = tid, price, remaining
            else:  # increase same direction
                new_qty = pos.qty + remaining
                pos.entry = (pos.entry * abs(pos.qty) + price * abs(remaining)) / abs(new_qty)
                pos.qty = new_qty
                tr = self.trade_map[pos.trade_id]
            tr.fees += fee
            tr.spread_cost += f_spread
            tr.slippage_cost += f_slip
            tr.max_qty = max(tr.max_qty, abs(pos.qty))
            tr.entry_notional = max(tr.entry_notional, abs(pos.qty) * pos.entry)
            tr.entry_price = pos.entry

    def _equity(self, prices: Dict[str, float]) -> float:
        u = sum(p.qty * (prices[s] - p.entry) for s, p in self.pos.items()
                if p.qty != 0 and np.isfinite(prices.get(s, np.nan)))
        return self.wallet + u

    def _mark(self, row_field: str, i: int) -> Dict[str, float]:
        out = {}
        for s in self.symbols:
            v = self.panel[s][row_field].iloc[i]
            if not np.isfinite(v):  # no bar: carry the last real close for marking only
                past = self.panel[s]["close"].iloc[:i + 1].dropna()
                v = past.iloc[-1] if len(past) else np.nan
            out[s] = float(v)
        return out

    # ── main loop ────────────────────────────────────────────────────────
    def run_backtest(self, start: Optional[str] = None, end: Optional[str] = None) -> BacktestResult:
        start = pd.Timestamp(start or self.run.start)
        end = pd.Timestamp(end or self.run.end)
        idx = self.panel[self.symbols[0]].index
        test_idx = [i for i, t in enumerate(idx) if start <= t < end]
        if not test_idx:
            raise ValueError("no bars inside the backtest window")
        self.wallet = float(self.run.initial_capital)
        self.pos = {s: Position(s) for s in self.symbols}
        self.fills, self.trades, self.trade_map = [], [], {}
        self._trade_ids = itertools.count(1)
        self.book = OrderBook()
        events, funding_flows, rejected, weights_hist, eq_rows = [], [], [], [], []
        cooldown = {s: 0 for s in self.symbols}
        peak = self.wallet
        day_start_eq, cur_day = self.wallet, None
        halted, halt_until_day, liquidated = None, None, False
        prev_bar_time = idx[test_idx[0]] - self.step
        last_i = test_idx[-1]

        last_equity = self.wallet
        for i in test_idx:
            t_open = idx[i]
            t_close = t_open + self.step
            if cur_day is None or t_open.normalize() != cur_day:
                cur_day, day_start_eq = t_open.normalize(), last_equity
            # 1. funding settlement
            t_ms_prev, t_ms = int(prev_bar_time.value // 1e6), int(t_open.value // 1e6)
            for s, p in self.pos.items():
                fdf = self.funding[s]
                if p.qty == 0 or fdf.empty:
                    continue
                ev = fdf[(fdf["timestamp"] > t_ms_prev) & (fdf["timestamp"] <= t_ms)]
                for _, r in ev.iterrows():
                    mark = r["mark_price"] if np.isfinite(r.get("mark_price", np.nan)) \
                        else self.panel[s]["open"].iloc[i]
                    if not np.isfinite(mark):
                        mark = p.entry
                    flow = C.funding_payment(p.qty, mark, float(r["funding_rate"]), self.costs)
                    self.wallet += flow
                    self.trade_map[p.trade_id].funding += flow
                    funding_flows.append({"time": str(pd.Timestamp(int(r["timestamp"]), unit="ms", tz="UTC")),
                                          "symbol": s, "qty": p.qty, "mark": mark,
                                          "rate": float(r["funding_rate"]), "flow": flow,
                                          "side": "long" if p.qty > 0 else "short"})
            prev_bar_time = t_open

            # 2. order execution at the open
            for o in self.book.pop_all():
                bar = self.panel[o.symbol].iloc[i]
                if not np.isfinite(bar["open"]):
                    rejected.append({"time": str(t_open), "symbol": o.symbol, "qty": o.qty,
                                     "reason": "no real bar to fill against"})
                    continue
                qty = o.qty
                prev_vol = self.panel[o.symbol]["volume"].iloc[i - 1] if i > 0 else np.nan
                cap = self.costs.max_bar_participation * prev_vol if np.isfinite(prev_vol) else np.inf
                partial = False
                if abs(qty) > cap:
                    qty = np.sign(qty) * C.round_qty(cap, self.specs[o.symbol])
                    partial = True
                    rejected.append({"time": str(t_open), "symbol": o.symbol, "qty": o.qty - qty,
                                     "reason": "partial fill: participation cap"})
                if qty != 0:
                    self._fill(self.pos[o.symbol], o, qty, float(bar["open"]), t_open, partial)

            # 3a. stop-losses
            for s, p in self.pos.items():
                if p.qty == 0:
                    continue
                bar = self.panel[s].iloc[i]
                if not np.isfinite(bar["low"]):
                    continue
                sl = self.risk.stop_loss_pct
                if p.qty > 0:
                    stop = p.entry * (1 - sl)
                    hit, px = bar["low"] <= stop, min(bar["open"], stop)
                else:
                    stop = p.entry * (1 + sl)
                    hit, px = bar["high"] >= stop, max(bar["open"], stop)
                if hit:
                    o = Order(-1, s, -p.qty, t_open, "stop_loss", True)
                    self._fill(p, o, -p.qty, float(px), t_open, False)
                    cooldown[s] = self.risk.stop_cooldown_bars
                    events.append({"time": str(t_open), "event": "stop_loss", "symbol": s,
                                   "price": float(px)})

            # 3b. cross-margin liquidation at adverse extremes
            adverse, mm = {}, 0.0
            for s, p in self.pos.items():
                if p.qty == 0:
                    continue
                bar = self.panel[s].iloc[i]
                px = bar["low"] if p.qty > 0 else bar["high"]
                if not np.isfinite(px):
                    px = self._mark("close", i)[s]
                adverse[s] = float(px)
                mm += C.maintenance_margin(abs(p.qty) * px, self.specs[s])
            if adverse:
                worst_eq = self.wallet + sum(self.pos[s].qty * (adverse[s] - self.pos[s].entry)
                                             for s in adverse)
                if worst_eq <= mm:
                    liquidated = True
                    for s, px in adverse.items():
                        p = self.pos[s]
                        liq_fee = abs(p.qty) * px * self.costs.liquidation_fee
                        o = Order(-1, s, -p.qty, t_open, "liquidation", True)
                        tr = self.trade_map[p.trade_id]
                        # liquidation engine fills at the adverse price, no extra spread
                        pnl = p.qty * (px - p.entry)
                        self.wallet += pnl - liq_fee
                        tr.gross_pnl += pnl
                        tr.fees += liq_fee
                        tr.exit_time, tr.exit_price, tr.exit_reason = t_open, px, "liquidation"
                        self.fills.append(Fill(o.id, t_open, s, "sell" if p.qty > 0 else "buy",
                                               abs(p.qty), px, px, abs(p.qty) * px, liq_fee,
                                               0.0, 0.0, "liquidation", False))
                        p.qty, p.entry, p.trade_id = 0.0, 0.0, None
                    self.wallet = max(self.wallet, 0.0)  # insurance fund absorbs deficit
                    events.append({"time": str(t_open), "event": "LIQUIDATION",
                                   "worst_equity": worst_eq, "maintenance_margin": mm})
                    halted = "liquidated"
                    self.book.pop_all()

            # 4. mark-to-market at the close
            closes = self._mark("close", i)
            equity = self._equity(closes)
            gross = sum(abs(p.qty) * closes[s] for s, p in self.pos.items() if p.qty != 0)
            peak = max(peak, equity)
            dd = 0.0 if peak <= 0 else (peak - equity) / peak
            eq_rows.append({"timestamp": t_close, "equity": equity, "wallet": self.wallet,
                            "unrealized": equity - self.wallet, "gross_exposure": gross,
                            "leverage_used": gross / equity if equity > 0 else np.nan,
                            "drawdown": dd})
            last_equity = equity
            for s in cooldown:
                cooldown[s] = max(0, cooldown[s] - 1)

            if i == last_i:
                break  # end-of-window close-out below
            if halted == "liquidated" or halted == "max_drawdown":
                continue
            if dd >= self.risk.max_drawdown_limit:
                halted = "max_drawdown"
                events.append({"time": str(t_close), "event": "EMERGENCY_SHUTDOWN",
                               "drawdown": dd})
                self._flatten(t_close, "max_drawdown_shutdown")
                continue
            if day_start_eq > 0 and equity <= day_start_eq * (1 - self.risk.daily_loss_limit):
                if halt_until_day != cur_day:
                    halt_until_day = cur_day
                    events.append({"time": str(t_close), "event": "DAILY_LOSS_HALT",
                                   "equity": equity, "day_start_equity": day_start_eq})
                    self._flatten(t_close, "daily_loss_halt")
                continue
            if halt_until_day is not None and halt_until_day == cur_day:
                continue

            # 5. decision at the close of bar i (data <= bar i only)
            hist = {s: self.panel[s].iloc[: i + 1] for s in self.symbols}
            decision_time = t_open  # rows are indexed by bar open; bar i is complete at t_close
            scan = self.scanner.scan(hist, decision_time, self.log)
            closes_hist = pd.DataFrame({s: hist[s]["close"] for s in self.symbols})
            mom = self.mom_agent.analyse(closes_hist, decision_time, self.log)
            fund = self.fund_agent.analyse(self.funding, t_close - pd.Timedelta(milliseconds=1), self.log)
            sig = strat.target_weights(closes_hist, self.funding,
                                       t_close - pd.Timedelta(milliseconds=1), self.cfg)
            # NB: funding with ts == t_close is settled at the next bar, it is
            # not yet known at the decision instant (strictly < t_close).
            dec = self.risk_agent.approve(
                sig["weights"], equity, decision_time, self.log, scan.eligible, cooldown,
                lambda sym, w: self.fund_agent.passes_filter(sym, w, fund["last_funding"]))
            tgt_w = pd.Series(dec.approved_weights)
            g = dec.gross_notional
            cur_w = pd.Series({s: (p.qty * closes[s] / g if g > 0 else 0.0)
                               for s, p in self.pos.items()})
            drift = strat.weight_drift(cur_w, tgt_w)
            sign_flip = any(np.sign(cur_w.get(s, 0)) != np.sign(tgt_w.get(s, 0))
                            and cur_w.get(s, 0) != 0 for s in self.symbols)
            do_rebalance = drift > self.cfg.rebalance_drift or sign_flip
            weights_hist.append({"time": str(t_close), "drift": drift, "rebalance": do_rebalance,
                                 **{f"w_{s}": tgt_w.get(s, 0.0) for s in self.symbols}})
            if do_rebalance:
                for s in self.symbols:
                    px = closes[s]
                    if not np.isfinite(px) or px <= 0:
                        continue
                    spec = self.specs[s]
                    target_qty = C.round_qty(tgt_w.get(s, 0.0) * g / px, spec)
                    if abs(target_qty) * px < spec.min_notional:
                        if tgt_w.get(s, 0.0) != 0:
                            rejected.append({"time": str(t_close), "symbol": s,
                                             "qty": tgt_w.get(s, 0.0) * g / px,
                                             "reason": f"below min notional {spec.min_notional} / lot step"})
                        target_qty = 0.0
                    delta = target_qty - self.pos[s].qty
                    if abs(delta) < spec.lot_step / 2:
                        continue
                    closing = target_qty == 0
                    if not closing and abs(delta) * px < spec.min_notional:
                        continue
                    self.book.submit(s, delta, t_close, "rebalance", reduce_only=closing)
                # pre-trade initial-margin check (risk manager hard rule)
                tot = sum(abs(self.pos[s].qty + (self.book.pending[s].qty if s in self.book.pending else 0))
                          * closes[s] for s in self.symbols)
                if equity > 0 and tot > self.risk.max_leverage * equity * 1.0001:
                    rejected.append({"time": str(t_close), "symbol": "*",
                                     "reason": "initial margin exceeded; orders cancelled"})
                    self.book.pop_all()

        # end of window: close everything at the final close
        t_end = idx[last_i] + self.step
        final = self._mark("close", last_i)
        for s, p in self.pos.items():
            if p.qty != 0 and np.isfinite(final[s]):
                o = Order(-1, s, -p.qty, t_end, "end_of_test", True)
                self._fill(p, o, -p.qty, final[s], t_end, False)
        eq_rows[-1].update({"equity": self.wallet, "wallet": self.wallet, "unrealized": 0.0,
                            "gross_exposure": 0.0, "leverage_used": 0.0})
        eq = pd.DataFrame(eq_rows).set_index("timestamp")
        start_row = pd.DataFrame([{"equity": self.run.initial_capital, "wallet": self.run.initial_capital,
                                   "unrealized": 0.0, "gross_exposure": 0.0, "leverage_used": 0.0,
                                   "drawdown": 0.0}], index=pd.DatetimeIndex([idx[test_idx[0]]], name="timestamp"))
        eq = pd.concat([start_row, eq])
        peak_curve = eq["equity"].cummax()
        eq["drawdown"] = np.where(peak_curve > 0, 1 - eq["equity"] / peak_curve, 0.0)
        return BacktestResult(self.run.to_dict(), eq, self.fills, self.trades, events,
                              liquidated, halted, funding_flows, rejected,
                              self.run.initial_capital, weights_hist)

    def _flatten(self, t, reason):
        self.book.pop_all()
        for s, p in self.pos.items():
            if p.qty != 0:
                self.book.submit(s, -p.qty, t, reason, reduce_only=True)
