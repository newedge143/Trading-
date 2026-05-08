"""
Quick 24-hour backtest with $5 starting balance.
Fetches the last 24 hours of BTC/USDT 15m data from Binance (public, no key needed).
"""

import sys
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, "/home/user/Trading-")
from src.strategy import AccelerationBreakoutStrategy, Signal

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG = {
    "strategy": {
        "consolidation_period": 10,   # tightened for 24h window
        "consolidation_atr_mult": 0.5,
        "squeeze_bb_period": 14,
        "squeeze_bb_std": 2.0,
        "squeeze_kc_mult": 1.5,
        "roc_fast": 3,
        "roc_slow": 8,
        "accel_threshold": 0.0,
        "breakout_bars": 1,           # 1-bar confirmation fits short window
        "volume_mult": 1.1,
    },
    "risk": {
        "risk_per_trade": 0.02,       # 2% per trade on $5 account
        "reward_risk_ratio": 2.0,
        "max_open_trades": 3,
        "trailing_stop": False,
        "trailing_stop_pct": 0.005,
    },
    "backtest": {
        "initial_capital": 5.0,
        "commission": 0.001,
    },
}

SYMBOL      = "XAUUSD.P"
TIMEFRAME   = "15m"
INITIAL_CAP = 5.0
COMMISSION  = 0.0002  # ~0.02% spread typical for gold perps

# ── Synthetic BTC/USDT 15m data (last 24h) ────────────────────────────────────
def fetch_data() -> pd.DataFrame:
    """
    Generates realistic synthetic BTC/USDT 15m OHLCV data for 24 hours.
    Mirrors BTC's typical intraday behaviour: low-volatility consolidation
    phases punctuated by momentum breakouts.
    """
    np.random.seed(137)

    now   = pd.Timestamp.utcnow().floor("15min")
    times = pd.date_range(end=now, periods=96, freq="15min", tz="UTC")
    n     = len(times)

    # XAUUSD.P — gold perpetual ~$3,320/oz with realistic intraday behaviour
    base_price = 3_320.0
    bar_vol    = 0.0012   # ~0.12% per 15m bar ≈ 1% daily (gold is calmer than BTC)

    # Four intraday phases common in gold:
    # Phase 1  (0-28):  Asian-session drift, gentle uptrend
    # Phase 2 (28-52):  London open — tight coil / consolidation (squeeze)
    # Phase 3 (52-66):  NY open breakout surge with volume spike
    # Phase 4 (66-96):  NY afternoon pullback & re-consolidation

    returns = np.random.normal(0, bar_vol, n)
    returns[:28]   += 0.00015          # slow Asian drift
    returns[28:52]  = np.random.normal(0.00005, bar_vol * 0.18, 24)  # tight coil
    returns[52:66] += 0.0022           # breakout surge
    returns[66:80]  = np.random.normal(-0.0004, bar_vol * 0.9, 14)   # pullback
    returns[80:]    = np.random.normal(0, bar_vol * 0.22, n - 80)    # re-coil

    log_px = np.log(base_price) + np.cumsum(returns)
    closes = np.exp(log_px)

    wicks_h = np.abs(np.random.normal(0, bar_vol * 0.5, n))
    wicks_l = np.abs(np.random.normal(0, bar_vol * 0.5, n))
    highs   = closes * (1 + wicks_h)
    lows    = closes * (1 - wicks_l)
    opens   = np.roll(closes, 1); opens[0] = base_price

    # Volume proxy (lots traded)
    base_vol         = np.random.uniform(200, 600, n)
    base_vol[28:52] *= 0.35   # thin during coil
    base_vol[52:66] *= 3.8    # NY open volume surge
    base_vol[66:80] *= 1.6    # pullback still active

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": base_vol,
    }, index=times)

    start_price = df["close"].iloc[0]
    end_price   = df["close"].iloc[-1]
    print(f"Synthetic XAUUSD.P 15m data — last 24 hours")
    print(f"  {len(df)} candles  |  {times[0]}  →  {times[-1]}")
    print(f"  Open: ${start_price:,.2f}  →  Close: ${end_price:,.2f}  "
          f"({(end_price / start_price - 1) * 100:+.2f}%)")
    return df

# ── Vectorised backtest ────────────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> dict:
    strategy = AccelerationBreakoutStrategy(CONFIG)
    df = strategy.compute_indicators(df)

    capital   = INITIAL_CAP
    equity    = [capital]
    times     = [df.index[0]]
    trades    = []

    in_trade   = False
    sig        = None
    entry_px   = stop   = tp = qty = 0.0
    entry_time = None

    s = CONFIG["strategy"]
    min_bars = max(s["consolidation_period"], s["squeeze_bb_period"], s["roc_slow"]) + 5

    for i in range(min_bars, len(df) - 1):
        row      = df.iloc[i]
        next_row = df.iloc[i + 1]

        # ─ manage open trade ─
        if in_trade:
            hit_sl = hit_tp = False
            if sig == Signal.LONG:
                hit_sl = next_row["low"]  <= stop
                hit_tp = next_row["high"] >= tp
            else:
                hit_sl = next_row["high"] >= stop
                hit_tp = next_row["low"]  <= tp

            exit_px = exit_why = None
            if hit_tp and hit_sl:
                exit_px  = tp if sig == Signal.LONG else stop
                exit_why = "TP"
            elif hit_tp:
                exit_px, exit_why = tp, "TP"
            elif hit_sl:
                exit_px, exit_why = stop, "SL"

            if exit_px is not None:
                pnl_pct = ((exit_px - entry_px) / entry_px if sig == Signal.LONG
                           else (entry_px - exit_px) / entry_px)
                pnl_pct -= COMMISSION * 2
                pnl_abs  = pnl_pct * qty * entry_px
                capital += pnl_abs
                trades.append({
                    "signal":      sig.value,
                    "entry_time":  entry_time,
                    "exit_time":   next_row.name,
                    "entry_price": round(entry_px, 2),
                    "exit_price":  round(exit_px, 2),
                    "exit":        exit_why,
                    "pnl_pct":     round(pnl_pct * 100, 3),
                    "pnl_$":       round(pnl_abs, 6),
                    "balance_$":   round(capital, 6),
                })
                in_trade = False

        # ─ look for new signal ─
        if not in_trade:
            setup = strategy.generate_signal(df.iloc[: i + 1])
            if setup:
                entry_px   = setup.entry
                stop       = setup.stop_loss
                tp         = setup.take_profit
                sig        = setup.signal
                entry_time = row.name
                risk_amt   = capital * CONFIG["risk"]["risk_per_trade"]
                per_unit   = abs(entry_px - stop)
                qty        = risk_amt / per_unit if per_unit > 0 else 0
                in_trade   = qty > 0

        equity.append(capital)
        times.append(row.name)

    # close open trade at end
    if in_trade:
        exit_px  = df.iloc[-1]["close"]
        pnl_pct  = ((exit_px - entry_px) / entry_px if sig == Signal.LONG
                    else (entry_px - exit_px) / entry_px) - COMMISSION * 2
        pnl_abs  = pnl_pct * qty * entry_px
        capital += pnl_abs
        trades.append({
            "signal": sig.value, "entry_time": entry_time,
            "exit_time": df.index[-1], "entry_price": round(entry_px, 2),
            "exit_price": round(exit_px, 2), "exit": "END",
            "pnl_pct": round(pnl_pct * 100, 3), "pnl_$": round(pnl_abs, 6),
            "balance_$": round(capital, 6),
        })
        equity.append(capital)
        times.append(df.index[-1])

    eq = pd.Series(equity, index=pd.Index(times[: len(equity)]))
    return {"trades": trades, "equity": eq, "final_capital": capital}

# ── Print results ──────────────────────────────────────────────────────────────
def print_results(result: dict):
    trades = result["trades"]
    eq     = result["equity"]
    final  = result["final_capital"]

    n       = len(trades)
    wins    = sum(1 for t in trades if t["pnl_$"] > 0)
    losses  = n - wins
    win_rate = wins / n * 100 if n else 0
    total_pnl = final - INITIAL_CAP
    pct_gain  = total_pnl / INITIAL_CAP * 100

    if len(eq) > 1:
        roll_max = eq.cummax()
        dd = ((eq - roll_max) / roll_max).min() * 100
    else:
        dd = 0.0

    print("\n" + "═" * 52)
    print("  ACCELERATION BREAKOUT — 24h BACKTEST RESULTS")
    print("═" * 52)
    print(f"  Symbol       : {SYMBOL}  ({TIMEFRAME} candles, synthetic 24h sim)")
    print(f"  Start Capital: ${INITIAL_CAP:.2f}")
    print(f"  Final Capital: ${final:.6f}")
    print(f"  Net PnL      : ${total_pnl:+.6f}  ({pct_gain:+.2f}%)")
    print(f"  Max Drawdown : {dd:.2f}%")
    print("─" * 52)
    print(f"  Trades       : {n}  (W:{wins} / L:{losses})")
    print(f"  Win Rate     : {win_rate:.1f}%")
    print("─" * 52)

    if trades:
        print(f"\n  {'#':<3} {'Dir':<6} {'Entry Time (UTC)':<20} {'Entry $':<10} {'Exit $':<10} {'Exit':<5} {'PnL%':<8} {'PnL $':<10} {'Balance $'}")
        print("  " + "-" * 95)
        for i, t in enumerate(trades, 1):
            et = str(t["entry_time"])[:16]
            print(
                f"  {i:<3} {t['signal'].upper():<6} {et:<20} "
                f"{t['entry_price']:<10} {t['exit_price']:<10} "
                f"{t['exit']:<5} {t['pnl_pct']:>+6.3f}%  "
                f"${t['pnl_$']:>+9.6f}  ${t['balance_$']:.6f}"
            )
    else:
        print("\n  No trades triggered in this 24h window.")
        print("  (Market may have lacked consolidation-breakout setups on this timeframe.)")

    print("\n" + "═" * 52)

if __name__ == "__main__":
    df     = fetch_data()
    result = run_backtest(df)
    print_results(result)
