#!/usr/bin/env python3
"""
BloFin Multi-Pair Scanner — Find the Best Pair for 1m Acceleration Breakout
============================================================================
Scans 15 top BloFin perpetual swaps and ranks them on what matters
for our scalping strategy:

  1. ATR % of price       — higher = more breakout opportunities
  2. Volume score         — recent volume vs 20-bar average (activity)
  3. Breakout frequency   — how often close > 10-bar high in last 100 bars
  4. Trend stability      — % of bars on same side of EMA20 (avoid choppy)
  5. Min lot affordability — can $10 account size positions cleanly

Run on the DO server (or anywhere with internet):
  source venv/bin/activate
  python3 scan_pairs.py
"""

import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import ccxt
import pandas as pd
import numpy as np

# Top liquid perpetuals on BloFin worth scanning for $10 scalping
PAIRS = [
    "XAU/USDT:USDT",   # Gold (current)
    "BTC/USDT:USDT",   # Bitcoin
    "ETH/USDT:USDT",   # Ethereum
    "SOL/USDT:USDT",   # Solana
    "XRP/USDT:USDT",   # Ripple
    "DOGE/USDT:USDT",  # Dogecoin
    "FET/USDT:USDT",   # Fetch.ai
    "AVAX/USDT:USDT",  # Avalanche
    "LINK/USDT:USDT",  # Chainlink
    "ADA/USDT:USDT",   # Cardano
    "TRX/USDT:USDT",   # Tron
    "SUI/USDT:USDT",   # Sui
    "PEPE/USDT:USDT",  # Pepe
    "WIF/USDT:USDT",   # WIF
    "TIA/USDT:USDT",   # Celestia
]

ACCOUNT = 10.00      # $10 account
RISK    = 0.02       # 2% risk
RR      = 3.0


def true_range(h, l, c):
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def atr(h, l, c, period=14):
    return true_range(h, l, c).ewm(alpha=1/period, min_periods=period, adjust=False).mean()


def ema(c, period=20):
    return c.ewm(span=period, adjust=False).mean()


def rolling_high(h, period=10):
    return h.rolling(period).max().shift(1)


def rolling_low(l, period=10):
    return l.rolling(period).min().shift(1)


def analyze_pair(ex, symbol):
    """Pull 100 1m candles and compute scoring metrics."""
    try:
        raw = ex.fetch_ohlcv(symbol, "1m", limit=100)
        if not raw or len(raw) < 50:
            return None
        df = pd.DataFrame(raw, columns=["ts", "o", "h", "l", "c", "v"]).astype(float)

        market = ex.market(symbol)
        min_amount  = market.get("limits", {}).get("amount", {}).get("min") or 0.0
        contract_sz = market.get("contractSize") or 1.0

        price        = float(df["c"].iloc[-1])
        atr14        = float(atr(df["h"], df["l"], df["c"], 14).iloc[-1])
        atr_pct      = (atr14 / price) * 100
        avg_vol_20   = float(df["v"].iloc[-20:].mean())
        last_vol     = float(df["v"].iloc[-1])
        vol_ratio    = last_vol / avg_vol_20 if avg_vol_20 > 0 else 0
        ema20        = ema(df["c"], 20)

        # Breakout count: bars where close > 10-bar high or close < 10-bar low
        rh = rolling_high(df["h"], 10)
        rl = rolling_low(df["l"], 10)
        breakouts = int(((df["c"] > rh) | (df["c"] < rl)).iloc[-100:].sum())

        # Trend stability: % of last 30 bars on same side of EMA20
        recent = df.iloc[-30:]
        above  = (recent["c"] > ema20.iloc[-30:]).sum()
        trend_pct = max(above, 30 - above) / 30 * 100

        # Affordability check
        sl_distance_usd = atr14 * 2.5
        risk_amount     = ACCOUNT * RISK
        ideal_qty       = risk_amount / sl_distance_usd if sl_distance_usd > 0 else 0
        min_qty_usd     = min_amount * price * contract_sz
        affordable      = ideal_qty >= min_amount and min_qty_usd <= ACCOUNT * 5  # 5x leverage cap

        return {
            "symbol":     symbol,
            "price":      price,
            "atr14":      atr14,
            "atr_pct":    atr_pct,
            "vol_ratio":  vol_ratio,
            "breakouts":  breakouts,
            "trend_pct":  trend_pct,
            "min_qty":    min_amount,
            "min_lot_$":  min_qty_usd,
            "affordable": affordable,
            "ideal_qty":  ideal_qty,
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)[:60]}


def score(r):
    """
    Combined score: more volatility + more breakouts + healthy volume.
    Penalize: too-extreme trend (>90% one-sided = exhausted), unaffordable lots.
    """
    if "error" in r:
        return -1
    if not r["affordable"]:
        return -1

    s = 0
    s += min(r["atr_pct"] * 25, 50)         # cap volatility score at 50
    s += min(r["breakouts"] * 0.6, 30)      # breakouts up to 30 pts
    s += 10 if 0.8 <= r["vol_ratio"] <= 3.5 else 0
    s += 10 if 55 <= r["trend_pct"] <= 80 else 0   # healthy directional bias
    return round(s, 1)


def main():
    print("\n" + "=" * 92)
    print("  BloFin Multi-Pair Scanner — Best 1m Scalping Candidates")
    print("=" * 92)
    print(f"  Account: ${ACCOUNT:.2f}   Risk: {RISK*100:.0f}%/trade   "
          f"R:R {RR:.0f}:1   Timeframe: 1m   Pairs scanned: {len(PAIRS)}")
    print("-" * 92)

    ex = ccxt.blofin({"options": {"defaultType": "swap"}})
    ex.load_markets()

    results = []
    for sym in PAIRS:
        if sym not in ex.markets:
            print(f"  [SKIP] {sym} not on BloFin")
            continue
        r = analyze_pair(ex, sym)
        if r is None:
            print(f"  [FAIL] {sym} - no data")
            continue
        results.append(r)
        if "error" in r:
            print(f"  [ERR ] {sym}  ->  {r['error']}")
        else:
            print(f"  [OK  ] {sym}  price=${r['price']:.4f}  ATR%={r['atr_pct']:.3f}  "
                  f"vol_ratio={r['vol_ratio']:.2f}  breakouts={r['breakouts']}")

    print()
    print("=" * 92)
    print(f"  {'#':<3} {'Pair':<22} {'Score':>6} {'ATR%':>7} {'Brkouts':>9} "
          f"{'VolR':>6} {'Trend%':>7} {'Min$':>8} {'Tradable?':>10}")
    print("  " + "-" * 88)

    valid = [r for r in results if "error" not in r]
    valid.sort(key=score, reverse=True)

    for i, r in enumerate(valid, 1):
        sc = score(r)
        ok = "YES" if r["affordable"] else "NO ($)"
        print(f"  {i:<3} {r['symbol']:<22} {sc:>6.1f} {r['atr_pct']:>6.3f}% "
              f"{r['breakouts']:>9} {r['vol_ratio']:>6.2f} {r['trend_pct']:>6.0f}% "
              f"${r['min_lot_$']:>6.2f} {ok:>10}")

    print()
    print("=" * 92)
    if valid:
        best = valid[0]
        print(f"  RECOMMENDATION:  {best['symbol']}")
        print(f"    - Volatility (ATR%): {best['atr_pct']:.3f}%  "
              f"({best['atr14']:.6f} per bar at ${best['price']:.4f})")
        print(f"    - Breakouts in last 100 bars: {best['breakouts']}")
        print(f"    - Volume ratio (now vs avg): {best['vol_ratio']:.2f}x")
        print(f"    - Min position: {best['min_qty']} contracts = ${best['min_lot_$']:.2f}")
        print(f"    - Estimated trades/day: {int(best['breakouts'] * 14.4)} "
              f"(scaled from 100 bars to 24h)")

        if len(valid) > 1:
            print(f"\n  RUNNERS-UP:")
            for r in valid[1:4]:
                print(f"    - {r['symbol']:<22} score={score(r):.1f}  "
                      f"ATR%={r['atr_pct']:.3f}  brkouts={r['breakouts']}")
    print("=" * 92)


if __name__ == "__main__":
    main()
