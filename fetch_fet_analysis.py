import ccxt
import sys

def compute_ema(closes, period):
    """Compute EMA for a list of closes."""
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def compute_atr(ohlcv, period=14):
    """Compute ATR(14) from ohlcv list."""
    true_ranges = []
    for i in range(1, len(ohlcv)):
        high = ohlcv[i][2]
        low  = ohlcv[i][3]
        prev_close = ohlcv[i-1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    # Simple moving average of last `period` TRs
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[-period:]) / period
    return atr

def main():
    print("=" * 55)
    print("  FET/USDT Perpetual Swap — BloFin Market Analysis")
    print("=" * 55)

    # Set up exchange
    ex = ccxt.blofin({"options": {"defaultType": "swap"}})
    print("\n[*] Loading markets from BloFin...")
    try:
        markets = ex.load_markets()
        print(f"    Loaded {len(markets)} markets OK")
    except Exception as e:
        print(f"    ERROR loading markets: {e}")
        sys.exit(1)

    # Try different symbol formats
    symbol_candidates = ["FET/USDT:USDT", "FETUSDT", "FET/USDT"]
    ohlcv = None
    used_symbol = None

    for sym in symbol_candidates:
        print(f"\n[*] Trying symbol: {sym}")
        try:
            ohlcv = ex.fetch_ohlcv(sym, timeframe="1m", limit=100)
            if ohlcv and len(ohlcv) > 0:
                used_symbol = sym
                print(f"    SUCCESS — got {len(ohlcv)} candles")
                break
            else:
                print(f"    Got empty response")
        except Exception as e:
            print(f"    FAILED: {e}")

    if not ohlcv or len(ohlcv) < 20:
        print("\n[!] Could not fetch sufficient OHLCV data from BloFin.")
        sys.exit(1)

    # Parse candles
    # Each candle: [timestamp, open, high, low, close, volume]
    closes  = [c[4] for c in ohlcv]
    highs   = [c[2] for c in ohlcv]
    lows    = [c[3] for c in ohlcv]
    volumes = [c[5] for c in ohlcv]

    current_price = closes[-1]

    # ATR(14)
    atr = compute_atr(ohlcv, period=14)
    atr_pct = (atr / current_price * 100) if atr else None

    # Average volume last 20 bars
    avg_vol_20 = sum(volumes[-20:]) / 20
    current_vol = volumes[-1]
    vol_ratio = current_vol / avg_vol_20 if avg_vol_20 else None

    # EMA(20) of closes
    ema20 = compute_ema(closes, 20)
    trending = "UP  (price > EMA20)" if current_price > ema20 else "DOWN (price < EMA20)"

    # Last 5 closes
    last5 = closes[-5:]

    print()
    print("=" * 55)
    print("  RESULTS")
    print("=" * 55)
    print(f"  Symbol used      : {used_symbol}")
    print(f"  Candles fetched  : {len(ohlcv)}")
    print()
    print(f"  1. Current Price : {current_price:.6f} USDT")
    print(f"  2. ATR(14)       : {atr:.6f} USDT" if atr else "  2. ATR(14)       : N/A")
    print(f"  3. ATR % of Price: {atr_pct:.4f}%" if atr_pct else "  3. ATR %         : N/A")
    print(f"  4. Avg Vol(20)   : {avg_vol_20:,.2f}")
    print(f"  5. Curr Vol      : {current_vol:,.2f}")
    print(f"     Vol Ratio     : {vol_ratio:.4f}x  ({'above' if vol_ratio >= 1 else 'below'} average)" if vol_ratio else "     Vol Ratio     : N/A")
    print(f"  6. Trend         : {trending}")
    print(f"     EMA20         : {ema20:.6f} USDT")
    print(f"  7. Last 5 Closes : {[round(c, 6) for c in last5]}")
    print()

    # Direction from last 5 closes
    if last5[-1] > last5[0]:
        dir_str = "Rising over last 5 bars"
    elif last5[-1] < last5[0]:
        dir_str = "Falling over last 5 bars"
    else:
        dir_str = "Flat over last 5 bars"
    print(f"     Recent Dir    : {dir_str}")
    print("=" * 55)

if __name__ == "__main__":
    main()
