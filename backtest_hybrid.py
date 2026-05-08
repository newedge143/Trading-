#!/usr/bin/env python3
"""
Backtest the Hybrid Regime-Aware Strategy on real BloFin 1m data.

The hybrid switches between trend-pullback and mean-reversion based on
detected market regime. Should perform better than either alone because
it stops trading the wrong strategy in the wrong conditions.

Usage:
  python3 backtest_hybrid.py
  TRADING_PAIR=ONDO/USDT:USDT python3 backtest_hybrid.py
"""

import os
import sys
import io
from collections import Counter
from pathlib import Path

import ccxt
import pandas as pd

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from src.hybrid_strategy import HybridStrategy, Signal

INITIAL    = 10.00
RISK_PCT   = 0.02
COMMISSION = 0.0012
SYMBOL_REQ = os.environ.get("TRADING_PAIR", "ONDO/USDT:USDT")
W = 80

CONFIG = {
    "regime": {
        "adx_period":      14,
        "ema_period":      200,
        "slope_lookback":  20,
        "adx_trend":       25.0,
        "adx_range":       18.0,
        "slope_threshold": 0.0008,
    },
    # Trend strategy (AccelerationBreakoutStrategy with EMA200+RSI rebuild)
    "trend": {
        "strategy": {
            "session_filter":            False,
            "sessions":                  [],
            "consolidation_period":      10,
            "consolidation_atr_mult":    1.0,
            "ema_fast_period":           50,
            "ema_slow_period":           200,
            "rsi_period":                14,
            "rsi_long_min":              50,
            "rsi_long_max":              75,
            "rsi_short_min":             25,
            "rsi_short_max":             50,
            "breakout_body_atr_mult":    0.5,
            "breakout_body_candle_ratio":0.50,
            "roc_fast":                  3,
            "roc_slow":                  8,
            "accel_threshold":           0.008,
            "volume_mult":               1.3,
        },
        "risk": {
            "atr_sl_mult":       1.5,
            "reward_risk_ratio": 3.0,
            "max_open_trades":   1,
            "trailing_stop":     False,
            "trailing_stop_pct": 0.003,
        },
    },
    # Range strategy (Scalp)
    "range": {
        "strategy": {
            "ema_period":      5,
            "adx_period":      14,
            "adx_min":         30.0,
            "stoch_k":         5,
            "stoch_d":         3,
            "stoch_smooth":    3,
            "stoch_oversold":  30.0,
            "stoch_overbought":70.0,
        },
        "risk": {
            "atr_sl_mult":       1.5,
            "reward_risk_ratio": 2.0,
        },
    },
}
RR_TREND = CONFIG["trend"]["risk"]["reward_risk_ratio"]


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
    raw = ex.fetch_ohlcv(symbol, "1m", limit=1500)
    df  = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.drop(columns=["ts"], inplace=True)
    return df.astype(float)


def simulate_trade(df, entry_idx, signal, sl, tp):
    for j in range(entry_idx + 1, len(df)):
        bar = df.iloc[j]
        if signal == Signal.LONG:
            if bar["low"]  <= sl: return "SL", sl, j - entry_idx
            if bar["high"] >= tp: return "TP", tp, j - entry_idx
        else:
            if bar["high"] >= sl: return "SL", sl, j - entry_idx
            if bar["low"]  <= tp: return "TP", tp, j - entry_idx
    return "OPEN", float(df.iloc[-1]["close"]), len(df) - entry_idx


def run_backtest(df, market):
    strategy   = HybridStrategy(CONFIG)
    balance    = INITIAL
    trades     = []
    skipped    = 0
    skip_until = 0
    regime_counter = Counter()
    min_amount  = market.get("limits", {}).get("amount", {}).get("min") or 0.0
    contract_sz = float(market.get("contractSize") or 1.0)

    warmup = max(220, len(df) - 1440)

    i = warmup
    while i < len(df) - 1:
        if i < skip_until:
            i += 1
            continue

        slice_ = df.iloc[: i + 1]
        setup  = strategy.generate_signal(slice_)
        regime_counter[strategy.last_regime.value if strategy.last_regime else "none"] += 1

        if setup is None or setup.signal == Signal.NONE:
            i += 1
            continue

        entry_idx = i + 1
        if entry_idx >= len(df):
            break
        entry_px = float(df.iloc[entry_idx]["open"])

        sl_dist = abs(setup.entry - setup.stop_loss)
        tp_dist = abs(setup.take_profit - setup.entry)
        if sl_dist <= 0:
            i += 1
            continue

        if setup.signal == Signal.LONG:
            sl = entry_px - sl_dist
            tp = entry_px + tp_dist
        else:
            sl = entry_px + sl_dist
            tp = entry_px - tp_dist

        risk_amt = balance * RISK_PCT
        qty      = risk_amt / sl_dist
        max_qty_margin = (balance * 20) / (entry_px * contract_sz)
        qty = min(qty, max_qty_margin)

        if min_amount and qty < min_amount:
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
        balance = max(balance + net_pnl, 0.001)

        trades.append({
            "n": len(trades) + 1, "ts": df.index[entry_idx],
            "regime": setup.reason.split("]")[0].lstrip("["),
            "dir": "LONG" if setup.signal == Signal.LONG else "SHORT",
            "entry": entry_px, "exit": exit_px, "outcome": outcome,
            "comm": comm, "net": net_pnl, "balance": balance, "bars": bars_held,
        })

        skip_until = entry_idx + bars_held + 1
        i = skip_until

    return trades, skipped, regime_counter


def display(trades, skipped, regime_counter, df, symbol):
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

    start_ts = df.index[max(220, len(df) - 1440)]
    end_ts   = df.index[-1]

    total_bars = sum(regime_counter.values())
    pct_trending = regime_counter.get("trending", 0) / total_bars * 100 if total_bars else 0
    pct_ranging  = regime_counter.get("ranging", 0)  / total_bars * 100 if total_bars else 0
    pct_trans    = regime_counter.get("transitional", 0) / total_bars * 100 if total_bars else 0

    print()
    print("=" * W)
    print(f"  HYBRID REGIME-AWARE 24H BACKTEST  --  {symbol}")
    print("=" * W)
    print(f"  Period    : {start_ts.strftime('%a %d %b %Y  %H:%M')} UTC  ->  "
          f"{end_ts.strftime('%a %d %b %Y  %H:%M')} UTC")
    print(f"  Strategy  : Hybrid (trend-pullback in trends, mean-reversion in chop)")
    print(f"  Risk      : {RISK_PCT*100:.0f}%/trade  |  Commission 0.12% RT  |  Lev 20x cap")
    print("-" * W)
    print(f"  Regime mix: TRENDING {pct_trending:.0f}%  RANGING {pct_ranging:.0f}%  "
          f"TRANSITIONAL {pct_trans:.0f}%")
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
        print(f"  {'#':<4} {'Time':<13} {'Regime':<11} {'Dir':<6} "
              f"{'Entry':>11} {'Exit':>11} {'Net':>9} {'Bal':>9} {'Held':>5}")
        print("  " + "-" * (W - 2))
        for t in trades:
            print(f"  {t['n']:<4} {t['ts'].strftime('%d %H:%M'):<13} "
                  f"{t['regime']:<11} {t['dir']:<6} "
                  f"{t['entry']:>11.6f} {t['exit']:>11.6f} "
                  f"${t['net']:>+8.4f} ${t['balance']:>7.4f} {t['bars']:>4}m  "
                  f"{t['outcome']}")

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
    print(f"  Trades/hour    : {n / 24:.2f}")
    print("=" * W)


def main():
    print(f"\n  Connecting to BloFin to fetch real {SYMBOL_REQ} 1m candles ...")
    ex = ccxt.blofin({"options": {"defaultType": "swap"}})
    ex.load_markets()
    sym = resolve_symbol(ex, SYMBOL_REQ)
    print(f"  Resolved symbol: {sym}")
    df  = fetch_24h(ex, sym)
    print(f"  Got {len(df)} candles  |  latest close: {df['close'].iloc[-1]}")
    market = ex.market(sym)
    print(f"  Min lot: {market.get('limits', {}).get('amount', {}).get('min')} contracts")
    print(f"  Running Hybrid strategy on the last 24h ...")
    trades, skipped, regimes = run_backtest(df, market)
    display(trades, skipped, regimes, df, sym)


if __name__ == "__main__":
    main()
