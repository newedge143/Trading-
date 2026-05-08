#!/usr/bin/env python3
"""
Backtest the TRUE Trend-Pullback Strategy on real BloFin data.
- 5m signals + 1h trend filter (multi-timeframe)
- Pullback to EMA20 then bounce (NOT breakout)
- Maker fees assumed (limit entry post-only)

Usage:
  python3 backtest_trend.py
  TRADING_PAIR=ONDO/USDT:USDT python3 backtest_trend.py
"""

import os
import sys
import io
from pathlib import Path

import ccxt
import pandas as pd

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from src.trend_pullback import TrendPullbackStrategy, Signal

INITIAL    = 10.00
RISK_PCT   = 0.05
COMMISSION = 0.0004     # maker round-trip
SYMBOL_REQ = os.environ.get("TRADING_PAIR", "ONDO/USDT:USDT")
W = 80

CONFIG = {
    "strategy": {
        "htf_ema_period":     50,
        "htf_slope_lookback": 5,
        "htf_slope_min_pct":  0.0005,   # catches more trends
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
        "atr_sl_mult":       1.2,       # tighter stop
        "reward_risk_ratio": 3.0,       # bigger TP target
    },
}

# Trailing stop config — once price moves favorably by trail_activate_rr × risk,
# the SL ratchets to (entry - trail_distance × ATR) and follows the move
TRAIL_ACTIVATE_RR    = 1.5    # activate trailing at 1.5×risk profit
TRAIL_DISTANCE_ATR   = 1.5    # trail this far below the local high (longs)


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


def fetch(ex, symbol: str, tf: str, limit: int) -> pd.DataFrame:
    raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df  = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.drop(columns=["ts"], inplace=True)
    return df.astype(float)


def simulate_trade(df, entry_idx, signal, entry_px, sl, tp, atr_at_entry):
    """Walk forward with trailing stop. Returns (outcome, exit_price, bars_held)."""
    initial_risk = abs(entry_px - sl)
    activate_at  = entry_px + TRAIL_ACTIVATE_RR * initial_risk if signal == Signal.LONG \
                   else entry_px - TRAIL_ACTIVATE_RR * initial_risk
    trailing_active = False
    trail_anchor    = entry_px   # local high (long) or low (short)

    for j in range(entry_idx + 1, len(df)):
        bar = df.iloc[j]
        if signal == Signal.LONG:
            # Update anchor and possibly the trailing SL
            if bar["high"] >= activate_at:
                trailing_active = True
            if trailing_active:
                trail_anchor = max(trail_anchor, bar["high"])
                trail_sl = trail_anchor - TRAIL_DISTANCE_ATR * atr_at_entry
                sl = max(sl, trail_sl)   # only ratchet up, never down
            if bar["low"] <= sl:
                return ("TP_TRAIL" if trailing_active else "SL"), sl, j - entry_idx
            if not trailing_active and bar["high"] >= tp:
                return "TP", tp, j - entry_idx
        else:  # SHORT
            if bar["low"] <= activate_at:
                trailing_active = True
            if trailing_active:
                trail_anchor = min(trail_anchor, bar["low"])
                trail_sl = trail_anchor + TRAIL_DISTANCE_ATR * atr_at_entry
                sl = min(sl, trail_sl)
            if bar["high"] >= sl:
                return ("TP_TRAIL" if trailing_active else "SL"), sl, j - entry_idx
            if not trailing_active and bar["low"] <= tp:
                return "TP", tp, j - entry_idx
    return "OPEN", float(df.iloc[-1]["close"]), len(df) - entry_idx


def run_backtest(df, htf_df, market):
    strategy   = TrendPullbackStrategy(CONFIG)
    balance    = INITIAL
    trades     = []
    skipped    = 0
    skip_until = 0
    min_amount  = market.get("limits", {}).get("amount", {}).get("min") or 0.0
    contract_sz = float(market.get("contractSize") or 1.0)

    bars_24h = int(24 * 60 / 5)
    warmup   = max(60, len(df) - bars_24h)

    i = warmup
    while i < len(df) - 1:
        if i < skip_until:
            i += 1
            continue

        setup = strategy.generate_signal(df.iloc[: i + 1], htf_df)
        if setup is None or setup.signal == Signal.NONE:
            i += 1
            continue

        entry_idx = i + 1
        if entry_idx >= len(df):
            break
        entry_px = float(df.iloc[entry_idx]["open"])

        sl_dist = abs(setup.entry - setup.stop_loss)
        tp_dist = abs(setup.take_profit - setup.entry)
        if sl_dist <= 0 or tp_dist <= 0:
            i += 1
            continue

        if setup.signal == Signal.LONG:
            sl = entry_px - sl_dist
            tp = entry_px + tp_dist
        else:
            sl = entry_px + sl_dist
            tp = entry_px - tp_dist

        risk_amt = balance * RISK_PCT
        qty = risk_amt / sl_dist
        max_qty_margin = (balance * 20) / (entry_px * contract_sz)
        qty = min(qty, max_qty_margin)

        if min_amount and qty < min_amount:
            skipped += 1
            i += 1
            continue

        pos_val = qty * entry_px * contract_sz
        comm    = pos_val * COMMISSION

        # Need ATR at entry for the trailing-stop distance — use last bar's ATR
        atr_at_entry = float(df.iloc[entry_idx - 1]["close"]) * 0  # placeholder, computed below
        # The strategy already computed it on idx-2 (via compute_indicators); recompute quickly
        from src.indicators import atr as _atr
        atr_series = _atr(df["high"], df["low"], df["close"], 14)
        atr_at_entry = float(atr_series.iloc[entry_idx - 1])
        if pd.isna(atr_at_entry) or atr_at_entry <= 0:
            atr_at_entry = sl_dist  # safe fallback

        outcome, exit_px, bars_held = simulate_trade(
            df, entry_idx, setup.signal, entry_px, sl, tp, atr_at_entry
        )

        direction = 1 if setup.signal == Signal.LONG else -1
        if outcome == "TP":
            gross = qty * tp_dist * contract_sz
        elif outcome == "SL":
            gross = -(qty * sl_dist * contract_sz)
        elif outcome == "TP_TRAIL":
            gross = qty * (exit_px - entry_px) * direction * contract_sz
        else:
            gross = qty * (exit_px - entry_px) * direction * contract_sz

        net_pnl = gross - comm
        balance = max(balance + net_pnl, 0.001)

        trades.append({
            "n": len(trades) + 1, "ts": df.index[entry_idx],
            "dir": "LONG" if setup.signal == Signal.LONG else "SHORT",
            "entry": entry_px, "exit": exit_px, "outcome": outcome,
            "comm": comm, "net": net_pnl, "balance": balance,
            "bars": bars_held, "minutes": bars_held * 5,
        })

        skip_until = entry_idx + bars_held + 1
        i = skip_until

    return trades, skipped


def display(trades, skipped, df, symbol):
    n = len(trades)
    wins   = [t for t in trades if t["outcome"] in ("TP", "TP_TRAIL") and t["net"] > 0]
    losses = [t for t in trades if t["outcome"] == "SL" or
              (t["outcome"] == "TP_TRAIL" and t["net"] <= 0)]
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

    print()
    print("=" * W)
    print(f"  TREND-PULLBACK BACKTEST  --  {symbol}")
    print("=" * W)
    print(f"  Strategy  : 1h trend filter + 5m EMA20 pullback bounce + RSI confirm")
    print(f"  Risk      : {RISK_PCT*100:.0f}%/trade  |  Maker fees 0.04% RT  |  RR 2.5:1")
    print("-" * W)
    print(f"  Start balance   : ${INITIAL:>9.4f}")
    print(f"  End balance     : ${final:>9.4f}")
    print(f"  Net profit      : ${profit:>+9.4f}  ({growth:+.2f}%)")
    print(f"  Trades taken    : {n}  [{len(wins)} TP / {len(losses)} SL / {len(opens)} OPEN]")
    print(f"  Skipped (under-min): {skipped}")
    print(f"  Win rate        : {wr:.1f}%")
    print(f"  Commission paid : ${total_comm:.4f}")
    print(f"  Max drawdown    : {max_dd:.2f}%")
    print("=" * W)

    if trades:
        print()
        print(f"  {'#':<4} {'Time':<13} {'Dir':<6} {'Entry':>11} {'Exit':>11} "
              f"{'Net':>9} {'Bal':>9} {'Held':>5}")
        print("  " + "-" * (W - 2))
        for t in trades:
            print(f"  {t['n']:<4} {t['ts'].strftime('%d %H:%M'):<13} {t['dir']:<6} "
                  f"{t['entry']:>11.6f} {t['exit']:>11.6f} "
                  f"${t['net']:>+8.4f} ${t['balance']:>7.4f} {t['minutes']:>4}m  {t['outcome']}")

    print()
    print("=" * W)
    print("  RISK METRICS")
    print("-" * W)
    if wins:
        avg_win = sum(t["net"] for t in wins) / len(wins)
        print(f"  Avg win        : ${avg_win:+.4f}")
    if losses:
        avg_loss = sum(t["net"] for t in losses) / len(losses)
        print(f"  Avg loss       : ${avg_loss:+.4f}")
    if wins and losses:
        gross_win  = sum(t["net"] for t in wins)
        gross_loss = abs(sum(t["net"] for t in losses))
        pf = gross_win / gross_loss if gross_loss else 0
        print(f"  Profit factor  : {pf:.2f}x")
    if n:
        ev = sum(t["net"] for t in trades) / n
        print(f"  Expected value : ${ev:+.4f}/trade")
    print(f"  Trades/day     : {n}")
    print("=" * W)


def main():
    print(f"\n  Connecting to BloFin to fetch real {SYMBOL_REQ} data ...")
    ex = ccxt.blofin({"options": {"defaultType": "swap"}})
    ex.load_markets()
    sym = resolve_symbol(ex, SYMBOL_REQ)
    print(f"  Resolved symbol: {sym}")
    df_5m = fetch(ex, sym, "5m", 1500)
    df_1h = fetch(ex, sym, "1h", 500)
    print(f"  Got {len(df_5m)} 5m candles, {len(df_1h)} 1h candles")
    print(f"  Latest 5m close: {df_5m['close'].iloc[-1]}")
    market = ex.market(sym)
    print(f"  Min lot: {market.get('limits', {}).get('amount', {}).get('min')} contracts")
    print(f"  Running Trend-Pullback strategy on the last 24h ...")
    trades, skipped = run_backtest(df_5m, df_1h, market)
    display(trades, skipped, df_5m, sym)


if __name__ == "__main__":
    main()
