"""
XAUUSD.P — 24h max-trades backtest, $5 starting balance.
Same strategy core, session filter OFF, limits relaxed to capture every valid signal.
"""

import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/Trading-")
from src.strategy import AccelerationBreakoutStrategy, Signal

# ── Max-trades config ──────────────────────────────────────────────────────────
CONFIG = {
    "strategy": {
        # Session filter OFF → trade every squeeze/breakout in the full 24h
        "session_filter": False,
        "sessions": [],
        # Consolidation — same BB(20,2)/KC(20,1.5) core, squeeze floor lowered to 3
        "consolidation_period": 20,
        "consolidation_atr_mult": 0.6,
        "squeeze_bb_period": 20,
        "squeeze_bb_std": 2.0,
        "squeeze_kc_mult": 1.5,
        "min_squeeze_bars": 3,          # was 6 — still requires meaningful coil
        # Candle body quality — kept (prevents wick traps)
        "breakout_body_atr_mult": 0.8,
        "breakout_body_candle_ratio": 0.60,
        # Momentum — threshold lowered to 0 so every acceleration fires
        "roc_fast": 3,
        "roc_slow": 8,
        "accel_threshold": 0.0,
        # Confirmation — 1-bar close outside range
        "breakout_bars": 1,
        "volume_mult": 1.2,             # was 1.5 — slightly looser, still real vol
        # Trend filter kept
        "ema_trend_period": 50,
    },
    "risk": {
        "risk_per_trade": 0.02,         # 2% per trade
        "atr_sl_mult": 2.5,
        "reward_risk_ratio": 3.0,       # 3:1 R/R
        "max_open_trades": 10,          # was 2 — allow stacking
        "trailing_stop": False,
        "trailing_stop_pct": 0.003,
    },
    "backtest": {
        "initial_capital": 5.0,
        "commission": 0.0002,
    },
}

SYMBOL      = "XAUUSD.P"
TIMEFRAME   = "5m"
INITIAL_CAP = 5.0
COMMISSION  = 0.0002

# ── Richer synthetic data: 6 squeeze-breakout cycles in 24h ───────────────────
def make_data() -> pd.DataFrame:
    """
    288 bars of 5m XAUUSD.P with SIX distinct coil→breakout events spread
    across the full day so the bot has ample opportunities to trade.
    """
    np.random.seed(7)
    now   = pd.Timestamp.now("UTC").floor("5min")
    times = pd.date_range(end=now, periods=288, freq="5min", tz="UTC")
    n     = len(times)
    base  = 3320.0
    bv    = 0.0008      # ~0.08% per 5m bar

    returns = np.random.normal(0, bv, n)

    # Six coil → breakout events, alternating LONG and SHORT
    events = [
        # (coil_start, coil_end, break_start, break_end, direction, magnitude)
        (10,  30,  30,  45,  +1,  0.0020),   # early Asian LONG
        (50,  70,  70,  82,  -1,  0.0018),   # Asian SHORT
        (88, 108, 108, 122,  +1,  0.0022),   # London open LONG
        (130, 150, 150, 162,  -1,  0.0016),  # London mid SHORT
        (168, 188, 188, 202,  +1,  0.0025),  # NY open LONG
        (215, 235, 235, 250,  -1,  0.0019),  # NY afternoon SHORT
    ]

    for coil_s, coil_e, brk_s, brk_e, dirn, mag in events:
        # coil = very low volatility
        returns[coil_s:coil_e] = np.random.normal(0, bv * 0.12, coil_e - coil_s)
        # breakout burst
        returns[brk_s:brk_e]  += dirn * mag

    log_px = np.log(base) + np.cumsum(returns)
    closes = np.exp(log_px)

    wh = np.abs(np.random.normal(0, bv * 0.35, n))
    wl = np.abs(np.random.normal(0, bv * 0.35, n))
    highs = closes * (1 + wh)
    lows  = closes * (1 - wl)
    opens = np.roll(closes, 1); opens[0] = base

    vol = np.random.uniform(100, 350, n)
    for coil_s, coil_e, brk_s, brk_e, _, _ in events:
        vol[coil_s:coil_e] *= 0.25   # thin during coil
        vol[brk_s:brk_e]   *= 4.0    # surge on breakout

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vol,
    }, index=times)

    p0, p1 = df["close"].iloc[0], df["close"].iloc[-1]
    print(f"Synthetic {SYMBOL} {TIMEFRAME} — 24h, 6 squeeze/breakout cycles")
    print(f"  {n} candles  |  {times[0]}  →  {times[-1]}")
    print(f"  Open: ${p0:,.2f}  →  Close: ${p1:,.2f}  ({(p1/p0-1)*100:+.2f}%)")
    return df

# ── Backtest engine ────────────────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> dict:
    strategy  = AccelerationBreakoutStrategy(CONFIG)
    risk_frac = CONFIG["risk"]["risk_per_trade"]
    max_trades = CONFIG["risk"]["max_open_trades"]

    df_ind = strategy.compute_indicators(df)
    capital = INITIAL_CAP
    equity  = [capital]
    eq_times = [df.index[0]]
    trades  = []

    # Support multiple concurrent open trades
    open_trades = []   # list of {sig, entry_px, stop, tp, qty, entry_time}

    s = CONFIG["strategy"]
    min_bars = max(
        s["consolidation_period"], s["squeeze_bb_period"],
        s["roc_slow"], s["ema_trend_period"]
    ) + 5

    for i in range(min_bars, len(df_ind) - 1):
        row      = df_ind.iloc[i]
        next_row = df_ind.iloc[i + 1]

        # ─ manage all open trades ─
        still_open = []
        for ot in open_trades:
            hit_sl = hit_tp = False
            if ot["sig"] == Signal.LONG:
                hit_sl = next_row["low"]  <= ot["stop"]
                hit_tp = next_row["high"] >= ot["tp"]
            else:
                hit_sl = next_row["high"] >= ot["stop"]
                hit_tp = next_row["low"]  <= ot["tp"]

            exit_px = exit_why = None
            if hit_tp and hit_sl:
                exit_px  = ot["tp"] if ot["sig"] == Signal.LONG else ot["stop"]
                exit_why = "TP"
            elif hit_tp:
                exit_px, exit_why = ot["tp"], "TP"
            elif hit_sl:
                exit_px, exit_why = ot["stop"], "SL"

            if exit_px is not None:
                pnl_pct = (
                    (exit_px - ot["entry_px"]) / ot["entry_px"]
                    if ot["sig"] == Signal.LONG
                    else (ot["entry_px"] - exit_px) / ot["entry_px"]
                ) - COMMISSION * 2
                pnl_abs  = pnl_pct * ot["qty"] * ot["entry_px"]
                capital += pnl_abs
                trades.append({
                    "signal":      ot["sig"].value,
                    "entry_time":  ot["entry_time"],
                    "exit_time":   next_row.name,
                    "entry_price": round(ot["entry_px"], 2),
                    "exit_price":  round(exit_px, 2),
                    "exit":        exit_why,
                    "pnl_pct":     round(pnl_pct * 100, 3),
                    "pnl_$":       round(pnl_abs, 6),
                    "balance_$":   round(capital, 6),
                })
            else:
                still_open.append(ot)
        open_trades = still_open

        # ─ new signal if below concurrent cap ─
        if len(open_trades) < max_trades:
            setup = strategy.generate_signal(df_ind.iloc[: i + 1])
            if setup:
                risk_amt = capital * risk_frac
                per_unit = abs(setup.entry - setup.stop_loss)
                qty = risk_amt / per_unit if per_unit > 0 else 0
                if qty > 0:
                    open_trades.append({
                        "sig":        setup.signal,
                        "entry_px":   setup.entry,
                        "stop":       setup.stop_loss,
                        "tp":         setup.take_profit,
                        "qty":        qty,
                        "entry_time": row.name,
                    })

        equity.append(capital)
        eq_times.append(row.name)

    # close remaining open trades at end-of-day price
    last_price = df_ind.iloc[-1]["close"]
    for ot in open_trades:
        pnl_pct = (
            (last_price - ot["entry_px"]) / ot["entry_px"]
            if ot["sig"] == Signal.LONG
            else (ot["entry_px"] - last_price) / ot["entry_px"]
        ) - COMMISSION * 2
        pnl_abs  = pnl_pct * ot["qty"] * ot["entry_px"]
        capital += pnl_abs
        trades.append({
            "signal": ot["sig"].value, "entry_time": ot["entry_time"],
            "exit_time": df_ind.index[-1], "entry_price": round(ot["entry_px"], 2),
            "exit_price": round(last_price, 2), "exit": "END",
            "pnl_pct": round(pnl_pct * 100, 3), "pnl_$": round(pnl_abs, 6),
            "balance_$": round(capital, 6),
        })
    equity.append(capital)
    eq_times.append(df_ind.index[-1])

    eq = pd.Series(equity, index=pd.Index(eq_times[: len(equity)]))
    return {"trades": trades, "equity": eq, "final_capital": capital}

# ── Results printer ────────────────────────────────────────────────────────────
def print_results(result: dict):
    trades = result["trades"]
    eq     = result["equity"]
    final  = result["final_capital"]

    n        = len(trades)
    wins     = sum(1 for t in trades if t["pnl_$"] > 0)
    losses   = n - wins
    win_rate = wins / n * 100 if n else 0
    total_pnl = final - INITIAL_CAP
    pct_gain  = total_pnl / INITIAL_CAP * 100

    avg_win  = (sum(t["pnl_$"] for t in trades if t["pnl_$"] > 0) / wins) if wins else 0
    avg_loss = (sum(t["pnl_$"] for t in trades if t["pnl_$"] <= 0) / losses) if losses else 0

    if len(eq) > 1:
        dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    else:
        dd = 0.0

    print("\n" + "═" * 62)
    print("  ACCELERATION BREAKOUT — MAX TRADES 24h RESULTS")
    print("═" * 62)
    print(f"  Symbol        : {SYMBOL}  ({TIMEFRAME} candles, 288 bars)")
    print(f"  Session filter: OFF  (all hours)")
    print(f"  Core filters  : BB(20,2)/KC(20,1.5) squeeze ≥3 bars")
    print(f"                  50 EMA trend | body ≥0.8×ATR & ≥60% | vol ≥1.2×")
    print(f"  Risk/Trade    : 2%  |  R:R = 3:1  |  Max concurrent: 10")
    print("─" * 62)
    print(f"  Start Capital : ${INITIAL_CAP:.2f}")
    print(f"  Final Capital : ${final:.4f}")
    print(f"  Net PnL       : ${total_pnl:+.4f}  ({pct_gain:+.2f}%)")
    print(f"  Max Drawdown  : {dd:.2f}%")
    print("─" * 62)
    print(f"  Total Trades  : {n}")
    print(f"  Winners       : {wins}   Losers: {losses}")
    print(f"  Win Rate      : {win_rate:.1f}%")
    print(f"  Avg Win       : ${avg_win:+.5f}")
    print(f"  Avg Loss      : ${avg_loss:+.5f}")
    print("─" * 62)

    if trades:
        print(f"\n  {'#':<3} {'Dir':<6} {'Entry (UTC)':<18} {'Entry':>8} {'Exit':>8} "
              f"{'Ex':<4} {'PnL%':>7}  {'PnL$':>10}  {'Bal$':>9}")
        print("  " + "─" * 82)
        for i, t in enumerate(trades, 1):
            et  = str(t["entry_time"])[:16]
            bar = "▲" if t["signal"] == "long" else "▼"
            print(
                f"  {i:<3} {bar}{t['signal'].upper():<5} {et:<18} "
                f"{t['entry_price']:>8.2f} {t['exit_price']:>8.2f} "
                f"{t['exit']:<4} {t['pnl_pct']:>+6.3f}%  "
                f"${t['pnl_$']:>+9.5f}  ${t['balance_$']:>9.4f}"
            )
    else:
        print("\n  No trades triggered.")

    print("\n" + "═" * 62)


if __name__ == "__main__":
    df     = make_data()
    result = run_backtest(df)
    print_results(result)
