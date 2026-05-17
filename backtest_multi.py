#!/usr/bin/env python3
"""
Multi-Pair Trend-Pullback Backtest
====================================
Runs the TrendPullbackStrategy on multiple BloFin pairs SIMULTANEOUSLY
sharing one account balance (the way the live bot will).

Position sizing: 5% risk per trade of TOTAL balance.
Max concurrent positions: 2 (so we never over-margin).
Same trailing-stop logic as backtest_trend.py.

Usage:
  python3 backtest_multi.py
  PAIRS=SUI,SOL,ONDO python3 backtest_multi.py
"""

import os
import sys
import io
import heapq
from pathlib import Path

import ccxt
import pandas as pd

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from src.trend_pullback import TrendPullbackStrategy, Signal
from src.indicators import atr as _atr

INITIAL    = 10.00
RISK_PCT   = 0.05
COMMISSION = 0.0004
MAX_OPEN   = 2
W = 84

DEFAULT_PAIRS = "SUI,SOL,ONDO"
PAIRS_REQ = os.environ.get("PAIRS", DEFAULT_PAIRS).split(",")

CONFIG = {
    "strategy": {
        "htf_ema_period":     50,
        "htf_slope_lookback": 5,
        "htf_slope_min_pct":  0.0005,
        "ema_fast":           20,
        "ema_slow":           50,
        "pullback_lookback":  8,
        "pullback_atr_mult":  0.5,
        "rsi_period":         14,
        "rsi_long_min":       42,
        "rsi_long_max":       75,
        "rsi_short_min":      25,
        "rsi_short_max":      58,
        "volume_mult":        0.8,
    },
    "risk": {
        "atr_sl_mult":       1.2,
        "reward_risk_ratio": 3.0,
    },
}
TRAIL_ACTIVATE_RR  = 1.5
TRAIL_DISTANCE_ATR = 1.5


def resolve_symbol(ex, requested: str) -> str:
    base = requested.strip().upper()
    candidates = [f"{base}/USDT:USDT", f"{base}USDT:USDT"]
    for c in candidates:
        if c in ex.markets:
            return c
    raise RuntimeError(f"No swap market for {requested}")


def fetch(ex, symbol: str, tf: str, limit: int) -> pd.DataFrame:
    raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df  = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.drop(columns=["ts"], inplace=True)
    return df.astype(float)


def main():
    print(f"\n  Connecting to BloFin ...")
    ex = ccxt.blofin({"options": {"defaultType": "swap"}})
    ex.load_markets()

    pair_state = {}
    for p in PAIRS_REQ:
        try:
            sym = resolve_symbol(ex, p)
        except RuntimeError as e:
            print(f"  [SKIP] {p}: {e}")
            continue

        df_5m = fetch(ex, sym, "5m", 1500)
        df_1h = fetch(ex, sym, "1h", 500)
        market = ex.market(sym)
        atr_series = _atr(df_5m["high"], df_5m["low"], df_5m["close"], 14)
        pair_state[sym] = {
            "df_5m":     df_5m,
            "df_1h":     df_1h,
            "atr":       atr_series,
            "min_amt":   market.get("limits", {}).get("amount", {}).get("min") or 0.0,
            "csz":       float(market.get("contractSize") or 1.0),
            "strategy":  TrendPullbackStrategy(CONFIG),
            "open":      None,
            "trades":    [],
            "skipped":   0,
        }
        print(f"  Loaded {sym}  ({len(df_5m)} 5m bars, {len(df_1h)} 1h bars)  "
              f"min lot={pair_state[sym]['min_amt']}  csz={pair_state[sym]['csz']}")

    if not pair_state:
        print("  No pairs loaded. Exiting.")
        return

    # Align all pairs to a common 5m bar index — use the last 288 bars (24h)
    common_index = None
    for s, st in pair_state.items():
        idx = st["df_5m"].index[-288:]
        common_index = idx if common_index is None else common_index.intersection(idx)
    print(f"  Common 24h window: {len(common_index)} 5m bars")

    balance = INITIAL
    bal_history = [(common_index[0], balance)]

    # Walk forward bar by bar
    for ts in common_index:
        # 1. CHECK EXITS for open positions
        for sym, st in pair_state.items():
            pos = st["open"]
            if pos is None:
                continue
            if ts not in st["df_5m"].index:
                continue
            bar = st["df_5m"].loc[ts]

            sl  = pos["sl"]
            tp  = pos["tp"]
            atr_e = pos["atr_at_entry"]

            # Trailing-stop ratchet
            if pos["side"] == "LONG":
                # Activate trailing if price hit activate_at
                if bar["high"] >= pos["activate_at"]:
                    pos["trailing"] = True
                if pos["trailing"]:
                    pos["anchor"] = max(pos["anchor"], bar["high"])
                    new_sl = pos["anchor"] - TRAIL_DISTANCE_ATR * atr_e
                    pos["sl"] = max(pos["sl"], new_sl)
                # Exit checks
                if bar["low"] <= pos["sl"]:
                    exit_px = pos["sl"]
                    outcome = "TP_TRAIL" if pos["trailing"] else "SL"
                    _close(st, balance_holder=None, exit_px=exit_px, outcome=outcome,
                           ts=ts, pos=pos, sym=sym)
                    balance = _settle(balance, st, pos, exit_px, outcome)
                    st["open"] = None
                    continue
                if not pos["trailing"] and bar["high"] >= pos["tp"]:
                    exit_px = pos["tp"]
                    outcome = "TP"
                    _close(st, None, exit_px, outcome, ts, pos, sym)
                    balance = _settle(balance, st, pos, exit_px, outcome)
                    st["open"] = None
                    continue
            else:  # SHORT
                if bar["low"] <= pos["activate_at"]:
                    pos["trailing"] = True
                if pos["trailing"]:
                    pos["anchor"] = min(pos["anchor"], bar["low"])
                    new_sl = pos["anchor"] + TRAIL_DISTANCE_ATR * atr_e
                    pos["sl"] = min(pos["sl"], new_sl)
                if bar["high"] >= pos["sl"]:
                    exit_px = pos["sl"]
                    outcome = "TP_TRAIL" if pos["trailing"] else "SL"
                    _close(st, None, exit_px, outcome, ts, pos, sym)
                    balance = _settle(balance, st, pos, exit_px, outcome)
                    st["open"] = None
                    continue
                if not pos["trailing"] and bar["low"] <= pos["tp"]:
                    exit_px = pos["tp"]
                    outcome = "TP"
                    _close(st, None, exit_px, outcome, ts, pos, sym)
                    balance = _settle(balance, st, pos, exit_px, outcome)
                    st["open"] = None
                    continue

        # 2. CHECK ENTRIES for pairs without open positions
        open_count = sum(1 for s in pair_state.values() if s["open"] is not None)
        if open_count >= MAX_OPEN:
            bal_history.append((ts, balance))
            continue

        for sym, st in pair_state.items():
            if st["open"] is not None:
                continue
            if open_count >= MAX_OPEN:
                break

            df_5m = st["df_5m"]
            if ts not in df_5m.index:
                continue

            # Pass only candles up to this ts (avoid look-ahead)
            sliced = df_5m.loc[:ts]
            setup = st["strategy"].generate_signal(sliced, st["df_1h"].loc[:ts])
            if setup is None or setup.signal == Signal.NONE:
                continue

            # Entry on next bar's open
            this_pos = list(df_5m.index).index(ts)
            if this_pos + 1 >= len(df_5m):
                continue
            entry_idx = this_pos + 1
            entry_px  = float(df_5m.iloc[entry_idx]["open"])

            sl_dist = abs(setup.entry - setup.stop_loss)
            tp_dist = abs(setup.take_profit - setup.entry)
            if sl_dist <= 0 or tp_dist <= 0:
                continue

            if setup.signal == Signal.LONG:
                sl = entry_px - sl_dist
                tp = entry_px + tp_dist
                activate_at = entry_px + TRAIL_ACTIVATE_RR * sl_dist
            else:
                sl = entry_px + sl_dist
                tp = entry_px - tp_dist
                activate_at = entry_px - TRAIL_ACTIVATE_RR * sl_dist

            risk_amt = balance * RISK_PCT
            qty      = risk_amt / sl_dist
            max_qty  = (balance * 20) / (entry_px * st["csz"])
            qty = min(qty, max_qty)

            if st["min_amt"] and qty < st["min_amt"]:
                st["skipped"] += 1
                continue

            atr_e = float(st["atr"].iloc[entry_idx - 1])
            if pd.isna(atr_e) or atr_e <= 0:
                atr_e = sl_dist

            st["open"] = {
                "side":         "LONG" if setup.signal == Signal.LONG else "SHORT",
                "entry_idx":    entry_idx,
                "entry_ts":     df_5m.index[entry_idx],
                "entry_px":     entry_px,
                "sl":           sl,
                "tp":           tp,
                "qty":          qty,
                "activate_at":  activate_at,
                "trailing":     False,
                "anchor":       entry_px,
                "atr_at_entry": atr_e,
            }
            open_count += 1

        bal_history.append((ts, balance))

    # Close any leftover open positions at last bar
    for sym, st in pair_state.items():
        pos = st["open"]
        if pos is None:
            continue
        last = st["df_5m"].iloc[-1]
        exit_px = float(last["close"])
        _close(st, None, exit_px, "OPEN", st["df_5m"].index[-1], pos, sym)
        balance = _settle(balance, st, pos, exit_px, "OPEN")
        st["open"] = None

    _display(pair_state, balance, bal_history)


def _close(st, _, exit_px, outcome, ts, pos, sym):
    st["trades"].append({
        "ts":      ts,
        "sym":     sym,
        "side":    pos["side"],
        "entry":   pos["entry_px"],
        "exit":    exit_px,
        "outcome": outcome,
        "qty":     pos["qty"],
    })


def _settle(balance, st, pos, exit_px, outcome):
    direction = 1 if pos["side"] == "LONG" else -1
    csz       = st["csz"]
    qty       = pos["qty"]
    gross     = qty * (exit_px - pos["entry_px"]) * direction * csz
    pos_val   = qty * pos["entry_px"] * csz
    comm      = pos_val * COMMISSION
    net       = gross - comm
    new_bal   = max(balance + net, 0.001)
    st["trades"][-1]["net"]     = net
    st["trades"][-1]["comm"]    = comm
    st["trades"][-1]["balance"] = new_bal
    return new_bal


def _display(pair_state, final_balance, bal_history):
    print()
    print("=" * W)
    print(f"  MULTI-PAIR TREND-PULLBACK BACKTEST")
    print("=" * W)
    print(f"  Pairs     : {', '.join(pair_state.keys())}")
    print(f"  Start     : ${INITIAL:.4f}    End: ${final_balance:.4f}    "
          f"Return: {(final_balance/INITIAL-1)*100:+.2f}%")
    print(f"  Risk      : {RISK_PCT*100:.0f}% of total balance per trade")
    print(f"  Max open  : {MAX_OPEN} concurrent positions")
    print(f"  Fees      : {COMMISSION*100:.2f}% RT (maker)")
    print("=" * W)

    all_trades = []
    for sym, st in pair_state.items():
        all_trades.extend([(t, sym) for t in st["trades"]])
    all_trades.sort(key=lambda x: x[0]["ts"])

    print()
    print(f"  {'#':<3} {'Time':<13} {'Pair':<22} {'Side':<6} {'Entry':>11} "
          f"{'Exit':>11} {'Net':>9} {'Bal':>9} {'Outcome':<10}")
    print("  " + "-" * (W - 2))
    for i, (t, sym) in enumerate(all_trades, 1):
        print(f"  {i:<3} {t['ts'].strftime('%d %H:%M'):<13} {sym:<22} {t['side']:<6} "
              f"{t['entry']:>11.6f} {t['exit']:>11.6f} "
              f"${t.get('net', 0):>+8.4f} ${t.get('balance', 0):>7.4f} {t['outcome']:<10}")

    print()
    print("=" * W)
    print("  PER-PAIR SUMMARY")
    print("-" * W)
    for sym, st in pair_state.items():
        n      = len(st["trades"])
        wins   = [t for t in st["trades"] if t["outcome"] in ("TP", "TP_TRAIL") and t.get("net", 0) > 0]
        losses = [t for t in st["trades"] if t["outcome"] == "SL" or
                  (t["outcome"] == "TP_TRAIL" and t.get("net", 0) <= 0)]
        wr = len(wins)/n*100 if n else 0
        net = sum(t.get("net", 0) for t in st["trades"])
        print(f"  {sym:<22} trades={n:<3}  WR={wr:>5.1f}%  net=${net:>+8.4f}  "
              f"skipped={st['skipped']}")

    print()
    print("=" * W)
    print("  AGGREGATE METRICS")
    print("-" * W)
    flat = [t for sym, st in pair_state.items() for t in st["trades"]]
    n = len(flat)
    wins   = [t for t in flat if t["outcome"] in ("TP", "TP_TRAIL") and t.get("net", 0) > 0]
    losses = [t for t in flat if t["outcome"] == "SL" or
              (t["outcome"] == "TP_TRAIL" and t.get("net", 0) <= 0)]
    wr = len(wins)/n*100 if n else 0
    print(f"  Trades total  : {n}  ({len(wins)} W / {len(losses)} L)")
    print(f"  Win rate      : {wr:.1f}%")
    if wins:
        print(f"  Avg win       : ${sum(t['net'] for t in wins)/len(wins):+.4f}")
    if losses:
        print(f"  Avg loss      : ${sum(t['net'] for t in losses)/len(losses):+.4f}")
    if wins and losses:
        gw = sum(t["net"] for t in wins)
        gl = abs(sum(t["net"] for t in losses))
        print(f"  Profit factor : {gw/gl if gl else 0:.2f}x")
    if n:
        print(f"  EV per trade  : ${sum(t.get('net', 0) for t in flat)/n:+.4f}")
    peak, max_dd = INITIAL, 0.0
    for ts, b in bal_history:
        peak   = max(peak, b)
        max_dd = min(max_dd, (b - peak) / peak * 100)
    print(f"  Max drawdown  : {max_dd:.2f}%")
    print("=" * W)


if __name__ == "__main__":
    main()
