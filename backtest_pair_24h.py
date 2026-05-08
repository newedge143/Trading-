#!/usr/bin/env python3
"""
Real-Data 24h Backtest — Any BloFin Pair
==========================================
Pulls the last 24h of REAL 1m candles from BloFin and runs the EXACT
same AccelerationBreakoutStrategy code the live bot uses.

This is what the bot would have done if it had been running for the
last 24 hours on this pair, with this $10 starting balance.

Usage (on DO server):
  source venv/bin/activate
  python3 backtest_pair_24h.py                 # default DOGE
  TRADING_PAIR=SUI/USDT:USDT python3 backtest_pair_24h.py
  TRADING_PAIR=SOL/USDT:USDT python3 backtest_pair_24h.py
"""

import os
import sys
import io
from pathlib import Path
from datetime import datetime, timezone

import ccxt
import pandas as pd
import numpy as np

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from src.strategy import AccelerationBreakoutStrategy, Signal

# ─── CONFIG (mirrors live_bot.py exactly) ────────────────────────────────────
INITIAL    = 10.00
RISK_PCT   = 0.02
RR         = 3.0
COMMISSION = 0.0012   # 0.12% round-trip BloFin taker
SYMBOL_REQ = os.environ.get("TRADING_PAIR", "DOGE/USDT:USDT")
W = 80

STRATEGY_CONFIG = {
    "strategy": {
        "session_filter": False,
        "sessions":       [],
        "consolidation_period":      10,
        "consolidation_atr_mult":    1.0,
        "squeeze_bb_period":         20,
        "squeeze_bb_std":            2.0,
        "squeeze_kc_mult":           1.5,
        "min_squeeze_bars":          2,
        "require_squeeze":           False,
        "breakout_body_atr_mult":    0.3,
        "breakout_body_candle_ratio":0.40,
        "roc_fast":                  3,
        "roc_slow":                  8,
        "accel_threshold":           0.005,
        "breakout_bars":             1,
        "volume_mult":               1.0,
        "ema_trend_period":          50,
    },
    "risk": {
        "risk_per_trade":    RISK_PCT,
        "atr_sl_mult":       2.5,
        "reward_risk_ratio": RR,
        "max_open_trades":   1,
        "trailing_stop":     False,
        "trailing_stop_pct": 0.003,
    },
}


# ─── DATA ────────────────────────────────────────────────────────────────────
def resolve_symbol(ex, requested: str) -> str:
    if requested in ex.markets:
        return requested
    base = requested.split("/")[0].split(":")[0].split("USDT")[0].upper()
    for c in [f"{base}/USDT:USDT", f"{base}USDT:USDT", f"{base}/USDT", f"{base}USDT"]:
        if c in ex.markets:
            return c
    for sym, mkt in ex.markets.items():
        if (mkt.get("base") or "").upper() == base and "swap" in (mkt.get("type") or "").lower():
            return sym
    raise RuntimeError(f"No market for {requested}")


def fetch_24h(ex, symbol: str) -> pd.DataFrame:
    """1500 bars (~25h) so 50 bars warmup + 1440 minutes of trading window."""
    raw = ex.fetch_ohlcv(symbol, "1m", limit=1500)
    df  = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.drop(columns=["ts"], inplace=True)
    return df.astype(float)


# ─── TRADE SIMULATION ────────────────────────────────────────────────────────
def simulate_trade(df, entry_idx, signal, sl, tp):
    """Walk forward bars after entry until SL or TP is hit (using real OHLC)."""
    for j in range(entry_idx + 1, len(df)):
        bar = df.iloc[j]
        if signal == Signal.LONG:
            if bar["low"] <= sl:
                return "SL", sl, j - entry_idx
            if bar["high"] >= tp:
                return "TP", tp, j - entry_idx
        else:
            if bar["high"] >= sl:
                return "SL", sl, j - entry_idx
            if bar["low"] <= tp:
                return "TP", tp, j - entry_idx
    return "OPEN", float(df.iloc[-1]["close"]), len(df) - entry_idx


# ─── BACKTEST ENGINE ─────────────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame, market: dict) -> tuple[list[dict], int]:
    strategy   = AccelerationBreakoutStrategy(STRATEGY_CONFIG)
    balance    = INITIAL
    trades     = []
    skipped    = 0
    skip_until = 0
    min_amount = market.get("limits", {}).get("amount", {}).get("min") or 0.0
    contract_sz = float(market.get("contractSize") or 1.0)

    # Trade only the last 1440 bars (24h); use bars 0..59 as warmup
    warmup = max(60, len(df) - 1440)

    i = warmup
    while i < len(df) - 1:
        if i < skip_until:
            i += 1
            continue

        setup = strategy.generate_signal(df.iloc[: i + 1])
        if setup is None or setup.signal == Signal.NONE:
            i += 1
            continue

        entry_idx = i + 1
        if entry_idx >= len(df):
            break
        entry_px = float(df.iloc[entry_idx]["open"])

        sl_dist = abs(setup.entry - setup.stop_loss)
        tp_dist = sl_dist * RR
        if sl_dist <= 0:
            i += 1
            continue

        if setup.signal == Signal.LONG:
            sl = entry_px - sl_dist
            tp = entry_px + tp_dist
        else:
            sl = entry_px + sl_dist
            tp = entry_px - tp_dist

        # Position sizing — same safety as fixed live_bot.calculate_qty
        risk_amt = balance * RISK_PCT
        qty      = risk_amt / sl_dist
        # Cap by 10x leverage on current balance (no over-leverage)
        max_qty_margin = (balance * 10) / (entry_px * contract_sz)
        qty = min(qty, max_qty_margin)

        if min_amount and qty < min_amount:
            # Below exchange min lot. Skip trade — never force over-leverage.
            skipped += 1
            i += 1
            continue

        pos_val = qty * entry_px * contract_sz
        comm    = pos_val * COMMISSION

        outcome, exit_px, bars_held = simulate_trade(df, entry_idx, setup.signal, sl, tp)

        if outcome == "TP":
            gross = qty * tp_dist * contract_sz
        elif outcome == "SL":
            gross = -(qty * sl_dist * contract_sz)
        else:
            direction = 1 if setup.signal == Signal.LONG else -1
            gross = qty * (exit_px - entry_px) * direction * contract_sz

        net_pnl = gross - comm
        old_bal = balance
        balance = max(balance + net_pnl, 0.001)

        trades.append({
            "n":         len(trades) + 1,
            "ts":        df.index[entry_idx],
            "dir":       "LONG" if setup.signal == Signal.LONG else "SHORT",
            "entry":     entry_px,
            "sl":        sl,
            "tp":        tp,
            "exit":      exit_px,
            "outcome":   outcome,
            "qty":       qty,
            "pos_val":   pos_val,
            "comm":      comm,
            "gross":     gross,
            "net":       net_pnl,
            "old_bal":   old_bal,
            "balance":   balance,
            "bars":      bars_held,
            "reason":    setup.reason,
        })

        skip_until = entry_idx + bars_held + 1
        i = skip_until

    return trades, skipped


# ─── DISPLAY ─────────────────────────────────────────────────────────────────
def display(trades, skipped, df, symbol):
    n      = len(trades)
    wins   = [t for t in trades if t["outcome"] == "TP"]
    losses = [t for t in trades if t["outcome"] == "SL"]
    opens  = [t for t in trades if t["outcome"] == "OPEN"]

    final  = trades[-1]["balance"] if trades else INITIAL
    profit = final - INITIAL
    growth = profit / INITIAL * 100
    wr     = len(wins) / n * 100 if n else 0
    total_comm = sum(t["comm"] for t in trades)

    peak, max_dd = INITIAL, 0.0
    for t in trades:
        peak   = max(peak, t["balance"])
        max_dd = min(max_dd, (t["balance"] - peak) / peak * 100)

    start_ts = df.index[max(60, len(df) - 1440)]
    end_ts   = df.index[-1]

    print()
    print("=" * W)
    print(f"  REAL-DATA 24H BACKTEST  --  {symbol}")
    print("=" * W)
    print(f"  Source    : LIVE BloFin 1m candles (just pulled)")
    print(f"  Period    : {start_ts.strftime('%a %d %b %Y  %H:%M')} UTC  ->  "
          f"{end_ts.strftime('%a %d %b %Y  %H:%M')} UTC")
    print(f"  Strategy  : Acceleration Breakout (require_squeeze=False, accel>0.005)")
    print(f"  Risk      : {RISK_PCT*100:.0f}%/trade  |  R:R {RR:.0f}:1  |  Commission 0.06%/side")
    print("-" * W)
    print(f"  Start balance   : ${INITIAL:>9.4f}")
    print(f"  End balance     : ${final:>9.4f}")
    print(f"  Net profit      : ${profit:>+9.4f}  ({growth:>+.2f}%)")
    print(f"  Trades taken    : {n}  [{len(wins)} TP / {len(losses)} SL / {len(opens)} OPEN]")
    print(f"  Skipped (under-min): {skipped}  (account too small for these signals)")
    print(f"  Win rate        : {wr:.1f}%")
    print(f"  Commission paid : ${total_comm:.4f}")
    print(f"  Max drawdown    : {max_dd:.2f}%")
    print("=" * W)

    if not n:
        print("\n  No signals fired in the last 24h on this pair.")
        print("  Try a different TRADING_PAIR — check scan_pairs.py output.")
        print("=" * W)
        return

    # Compact trade log
    print(f"\n  {'#':<4} {'Time UTC':<12} {'Dir':<6} "
          f"{'Entry':>10} {'Exit':>10} {'Net P&L':>9} {'Result':<10} {'Balance':>10}  Held")
    print("  " + "-" * 86)
    for t in trades[:80]:    # cap at 80 lines
        sign = "+" if t["net"] >= 0 else ""
        tag  = "TP [WIN]" if t["outcome"] == "TP" else ("SL [LOSS]" if t["outcome"]=="SL" else "OPEN")
        print(f"  {t['n']:<4} {t['ts'].strftime('%d %H:%M'):<12} "
              f"{t['dir']:<6} {t['entry']:>10.6f} {t['exit']:>10.6f} "
              f"{sign}${abs(t['net']):>7.4f} {tag:<10} ${t['balance']:>9.4f}  {t['bars']}m")
    if n > 80:
        print(f"  ... ({n - 80} more trades)")

    # Risk metrics
    print()
    print("=" * W)
    print("  RISK METRICS")
    print("-" * W)
    if wins:
        avg_w = sum(t["net"] for t in wins) / len(wins)
        print(f"  Avg win        : +${avg_w:.4f}")
    if losses:
        avg_l = sum(t["net"] for t in losses) / len(losses)
        print(f"  Avg loss       :  ${avg_l:.4f}")
    if wins and losses:
        pf = avg_w / abs(avg_l)
        ev = (wr/100)*avg_w + (1-wr/100)*avg_l
        print(f"  Profit factor  : {pf:.2f}x")
        print(f"  Expected value : ${ev:+.4f}/trade")
    print(f"  Max drawdown   : {max_dd:.2f}%")
    print(f"  Avg trade hold : {sum(t['bars'] for t in trades)/n:.1f} minutes")
    print(f"  Trades/hour    : {n/24:.1f}")

    # Projection
    if growth > 0:
        print()
        print("=" * W)
        print(f"  $10 -> $500 PROJECTION  ({wr:.0f}% WR, {n} trades/24h, +{growth:.2f}%/day)")
        print("-" * W)
        bal, day = INITIAL, 0
        while bal < 500 and day < 60:
            bal *= 1 + growth/100
            day += 1
            if day <= 7 or bal >= 500 or (bal >= 100 and bal/(1+growth/100) < 100):
                tag = " <-- TARGET" if bal >= 500 else ""
                print(f"  Day {day:<3}: ${bal:>10.4f}{tag}")
    print("=" * W)


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n  Connecting to BloFin to fetch real {SYMBOL_REQ} 1m candles ...")
    ex = ccxt.blofin({"options": {"defaultType": "swap"}})
    ex.load_markets()
    symbol = resolve_symbol(ex, SYMBOL_REQ)
    print(f"  Resolved symbol: {symbol}")

    df = fetch_24h(ex, symbol)
    print(f"  Got {len(df)} candles  |  latest close: {df['close'].iloc[-1]}")

    market = ex.market(symbol)
    print(f"  Min lot: {market.get('limits', {}).get('amount', {}).get('min')} contracts")
    print(f"  Running strategy on the last 24h ({min(1440, len(df))} bars) ...")

    trades, skipped = run_backtest(df, market)
    display(trades, skipped, df, symbol)
