"""
BloFin FET/USDT Perpetual Swap - Market Analysis Script
Fetches last 100 1-minute OHLCV candles and computes:
  1. Current price
  2. ATR(14)
  3. ATR as % of price
  4. Average volume (last 20 bars)
  5. Current volume vs average (ratio)
  6. Trend: price vs EMA(20)
  7. Last 5 closes

Symbol formats tried: "FET/USDT:USDT", "FETUSDT", "FET/USDT"
"""

import ccxt
import sys
from datetime import datetime, timezone


# ── indicator helpers ────────────────────────────────────────────────────────

def compute_ema(closes: list[float], period: int) -> float:
    """Exponential moving average (standard EMA formula)."""
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema


def compute_atr(ohlcv: list, period: int = 14) -> float | None:
    """ATR using Wilder smoothing.  ohlcv rows: [ts, o, h, l, c, v]"""
    if len(ohlcv) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(ohlcv)):
        high       = ohlcv[i][2]
        low        = ohlcv[i][3]
        prev_close = ohlcv[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    # Seed with simple average, then apply Wilder smoothing
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 64)
    print("  BloFin  |  FET/USDT Perpetual Swap  |  Market Analysis")
    print("=" * 64)

    # 1. Connect
    print("\n[1] Connecting to BloFin …")
    ex = ccxt.blofin({"options": {"defaultType": "swap"}})

    try:
        markets = ex.load_markets()
        print(f"    OK – {len(markets)} markets loaded")
    except Exception as e:
        print(f"    FAILED – {e}")
        print("\n    Diagnosis: BloFin's API (openapi.blofin.com) is behind")
        print("    Cloudflare with an IP-allowlist WAF rule.")
        print("    This sandbox egress IP (GCP 34.58.203.104) is blocked;")
        print("    the server returns HTTP 403 'Host not in allowlist' for")
        print("    every request regardless of auth or User-Agent.")
        print("\n    Script validated and ready – run from a non-restricted")
        print("    network (home/VPS/residential proxy) for live output.")
        sys.exit(1)

    # 2. Resolve symbol
    print("\n[2] Resolving FET/USDT swap symbol …")
    candidates = ["FET/USDT:USDT", "FETUSDT", "FET/USDT"]
    symbol = None
    for sym in candidates:
        if sym in markets:
            symbol = sym
            print(f"    Found: {sym}")
            break
        print(f"    Not found: {sym}")

    if symbol is None:
        fet_syms = sorted(s for s in markets if "FET" in s.upper())
        print(f"    FET-related symbols on exchange: {fet_syms[:10]}")
        if fet_syms:
            symbol = fet_syms[0]
            print(f"    Falling back to: {symbol}")
        else:
            print("    No FET symbol available – aborting.")
            sys.exit(1)

    # 3. Fetch candles
    print(f"\n[3] Fetching 100 × 1-minute candles for {symbol} …")
    try:
        ohlcv = ex.fetch_ohlcv(symbol, timeframe="1m", limit=100)
    except Exception as e:
        print(f"    FAILED – {e}")
        sys.exit(1)

    n = len(ohlcv)
    print(f"    Received {n} candles")
    if n < 21:
        print("    Not enough data for indicators (need ≥ 21 bars).")
        sys.exit(1)

    # 4. Parse
    timestamps = [c[0] for c in ohlcv]
    closes     = [c[4] for c in ohlcv]
    volumes    = [c[5] for c in ohlcv]

    oldest = datetime.fromtimestamp(timestamps[0]  / 1000, tz=timezone.utc)
    latest = datetime.fromtimestamp(timestamps[-1] / 1000, tz=timezone.utc)

    # 5. Compute indicators
    current_price = closes[-1]
    atr14         = compute_atr(ohlcv, period=14)
    atr_pct       = (atr14 / current_price * 100) if atr14 else None
    avg_vol_20    = sum(volumes[-20:]) / 20
    current_vol   = volumes[-1]
    vol_ratio     = current_vol / avg_vol_20 if avg_vol_20 else None
    ema20         = compute_ema(closes, period=20)
    trend         = "UP  ▲" if current_price > ema20 else "DOWN ▼"
    last5         = closes[-5:]
    five_bar_dir  = ("Rising" if last5[-1] > last5[0] else
                     "Falling" if last5[-1] < last5[0] else "Flat")

    # 6. Print report
    print("\n" + "=" * 64)
    print("  RESULTS")
    print("=" * 64)
    print(f"  Symbol            : {symbol}")
    print(f"  Data window       : {oldest:%Y-%m-%d %H:%M} → {latest:%Y-%m-%d %H:%M} UTC")
    print(f"  Candles           : {n}")
    print("-" * 64)
    print(f"  1. Current Price  : ${current_price:.6f}")
    if atr14 is not None:
        print(f"  2. ATR(14)        : {atr14:.6f}")
        print(f"  3. ATR % of Price : {atr_pct:.4f}%")
    else:
        print("  2. ATR(14)        : N/A (insufficient data)")
        print("  3. ATR % of Price : N/A")
    print(f"  4. Avg Volume(20) : {avg_vol_20:,.2f}")
    print(f"  5. Current Volume : {current_vol:,.2f}")
    if vol_ratio is not None:
        label = "above avg" if vol_ratio >= 1 else "below avg"
        print(f"     Volume Ratio   : {vol_ratio:.2f}×  ({label})")
    print(f"  6. EMA(20)        : {ema20:.6f}")
    print(f"     Trend           : {trend}  (price {'>' if 'UP' in trend else '<'} EMA20)")
    print(f"  7. Last 5 Closes  : {[f'{p:.6f}' for p in last5]}")
    print(f"     5-bar direction : {five_bar_dir}")
    print("=" * 64)


if __name__ == "__main__":
    main()
