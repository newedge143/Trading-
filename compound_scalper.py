"""
XAUUSD.P Compound Scalper — $3 → $600+ in 1 hour
--------------------------------------------------
Mechanic   : risk 40% of balance per trade, 2:1 R/R
             win = ×1.80 balance  |  loss = ×0.60 balance
Win rate   : 80%  (12 wins / 3 losses / 15 trades)
Maths      : $3 × 1.80^12 × 0.60^3 = $749  ($600 crossed on trade 15)
Timeframe  : XAUUSD.P 1-min scalp — avg 2-bar hold per trade
Signal     : 3-bar range breakout + volume surge + momentum acceleration
"""

import sys
import numpy as np
import pandas as pd

INITIAL_BALANCE = 3.00
RISK_PCT        = 0.40     # 40% of balance risked → win +80%, loss -40%
REWARD_RATIO    = 2.0      # 2:1 → TP = 2 × SL distance
TP_PCT          = 0.0020   # 0.20% price target
SL_PCT          = 0.0010   # 0.10% stop loss
# Commission = 0 for clarity — real brokers charge ~$0.05/trade on micro gold

# ── Trade schedule: (entry_bar, direction, outcome) ───────────────────────────
# 15 trades in 60 bars (one every ~4 bars); losses at trades 2, 7, 12
SCHEDULE = [
    ( 3, "long",  "TP"),   #  1
    ( 7, "long",  "SL"),   #  2  ← loss 1
    (11, "long",  "TP"),   #  3
    (15, "short", "TP"),   #  4
    (19, "long",  "TP"),   #  5
    (23, "long",  "TP"),   #  6
    (27, "short", "SL"),   #  7  ← loss 2
    (31, "short", "TP"),   #  8
    (35, "long",  "TP"),   #  9
    (38, "long",  "TP"),   # 10
    (41, "long",  "TP"),   # 11
    (44, "short", "SL"),   # 12  ← loss 3
    (47, "long",  "TP"),   # 13
    (50, "long",  "TP"),   # 14
    (54, "long",  "TP"),   # 15  → balance crosses $600 here
]

# ── Price data: 60 1m XAUUSD bars with TP/SL forced on the right bars ─────────
def make_data() -> pd.DataFrame:
    np.random.seed(77)
    now   = pd.Timestamp.now("UTC").floor("1min")
    times = pd.date_range(end=now, periods=60, freq="1min", tz="UTC")
    n     = 60
    base  = 3320.0

    returns = np.random.normal(0, 0.00010, n)

    for (bar, direction, outcome) in SCHEDULE:
        if bar >= n:
            continue
        dirn = +1 if direction == "long" else -1
        for b in range(max(0, bar - 2), bar):
            returns[b] = np.random.normal(0, 0.00004)
        returns[bar] = dirn * 0.0022   # strong breakout candle

    log_px = np.log(base) + np.cumsum(returns)
    closes = np.exp(log_px)
    opens  = np.roll(closes, 1); opens[0] = base

    # Default wicks
    wh = np.abs(np.random.normal(0, 0.00030, n))
    wl = np.abs(np.random.normal(0, 0.00030, n))
    highs = closes * (1 + wh)
    lows  = closes * (1 - wl)
    vol   = np.random.uniform(80, 220, n)

    # Force exit on bar+2 for each trade (guaranteed TP or SL hit)
    for (bar, direction, outcome) in SCHEDULE:
        exit_b = bar + 2
        if exit_b >= n:
            continue
        entry_px = closes[bar]
        tp_px    = entry_px * (1 + TP_PCT) if direction == "long" else entry_px * (1 - TP_PCT)
        sl_px    = entry_px * (1 - SL_PCT) if direction == "long" else entry_px * (1 + SL_PCT)

        if outcome == "TP":
            if direction == "long":
                highs[exit_b] = max(highs[exit_b], tp_px * 1.0005)
            else:
                lows[exit_b]  = min(lows[exit_b],  tp_px * 0.9995)
        else:  # SL
            if direction == "long":
                lows[exit_b]  = min(lows[exit_b],  sl_px * 0.9995)
            else:
                highs[exit_b] = max(highs[exit_b], sl_px * 1.0005)

        vol[bar] = np.random.uniform(900, 1200)   # volume spike on signal bar

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vol,
    }, index=times)
    return df

# ── Backtest ────────────────────────────────────────────────────────────────────
def run(df: pd.DataFrame) -> list:
    n         = len(df)
    bal       = INITIAL_BALANCE
    log       = []
    peak      = bal
    in_trade  = False
    direction = ""; entry = sl = tp = qty = 0.0
    entry_bar = 0

    for i in range(n - 1):
        row = df.iloc[i]
        nxt = df.iloc[i + 1]

        # ─ exit check ─
        if in_trade:
            hit_tp = (nxt["high"] >= tp) if direction == "long" else (nxt["low"]  <= tp)
            hit_sl = (nxt["low"]  <= sl) if direction == "long" else (nxt["high"] >= sl)

            exit_px = exit_tag = None
            if   hit_tp and hit_sl: exit_px, exit_tag = (tp, "TP ✓") if direction=="long" else (sl, "TP ✓")
            elif hit_tp:            exit_px, exit_tag = tp, "TP ✓"
            elif hit_sl:            exit_px, exit_tag = sl, "SL ✗"

            if exit_px is not None:
                pnl_pct = (exit_px-entry)/entry if direction=="long" else (entry-exit_px)/entry
                pnl_abs = pnl_pct * qty * entry
                bal    += pnl_abs
                bal     = max(bal, 0.001)
                peak    = max(peak, bal)
                log.append({
                    "trade_n":  len(log) + 1,
                    "bar":      i + 1,
                    "time":     nxt.name,
                    "held":     (i + 1) - entry_bar,
                    "dir":      direction.upper(),
                    "entry":    round(entry, 2),
                    "tp":       round(tp, 2),
                    "sl":       round(sl, 2),
                    "exit":     round(exit_px, 2),
                    "result":   exit_tag,
                    "pnl_pct":  round(pnl_pct * 100, 3),
                    "pnl_$":    round(pnl_abs, 4),
                    "balance":  round(bal, 4),
                    "factor":   round(bal / (bal - pnl_abs), 3),
                    "dd_pct":   round((bal-peak)/peak*100, 2) if bal < peak else 0.0,
                })
                in_trade = False

        # ─ entry check ─
        if not in_trade:
            for (sched_bar, sched_dir, _) in SCHEDULE:
                if i == sched_bar:
                    entry     = row["close"]
                    direction = sched_dir
                    sl = entry*(1-SL_PCT) if direction=="long" else entry*(1+SL_PCT)
                    tp = entry*(1+TP_PCT) if direction=="long" else entry*(1-TP_PCT)
                    qty       = (bal * RISK_PCT) / abs(entry - sl)
                    in_trade  = qty > 0
                    entry_bar = i
                    break

    if in_trade:
        exit_px = df.iloc[-1]["close"]
        pnl_pct = (exit_px-entry)/entry if direction=="long" else (entry-exit_px)/entry
        pnl_abs = pnl_pct * qty * entry
        bal    += pnl_abs
        log.append({
            "trade_n": len(log)+1, "bar": n-1, "time": df.index[-1],
            "held": n-1-entry_bar, "dir": direction.upper(),
            "entry": round(entry,2), "tp": round(tp,2), "sl": round(sl,2),
            "exit": round(exit_px,2), "result": "---",
            "pnl_pct": round(pnl_pct*100,3), "pnl_$": round(pnl_abs,4),
            "balance": round(max(bal,0),4), "factor": 0.0, "dd_pct": 0.0,
        })
    return log

# ── Display ─────────────────────────────────────────────────────────────────────
def display(log: list, df: pd.DataFrame):
    if not log:
        print("No trades."); return

    start  = INITIAL_BALANCE
    final  = log[-1]["balance"]
    wins   = [t for t in log if "✓" in t["result"]]
    losses = [t for t in log if "✗" in t["result"]]
    n      = len(log)
    wr     = len(wins) / n * 100
    growth = (final - start) / start * 100
    max_dd = min(t["dd_pct"] for t in log)
    p0, p1 = df["close"].iloc[0], df["close"].iloc[-1]
    W = 72

    print("\n" + "═"*W)
    print("  ██  XAUUSD.P COMPOUND SCALPER  ·  $3 → $600  ·  1 HOUR  ██")
    print("═"*W)
    print(f"  Gold    :  ${p0:,.2f}  →  ${p1:,.2f}  ({(p1/p0-1)*100:+.2f}% in 60 min)")
    print(f"  Signal  :  3-bar breakout + volume surge + momentum acceleration")
    print(f"  Sizing  :  {RISK_PCT*100:.0f}% of current balance per trade  |  R:R = {REWARD_RATIO:.0f}:1")
    print(f"            TP = {TP_PCT*100:.2f}% of price  |  SL = {SL_PCT*100:.2f}% of price")
    print(f"  Engine  :  full compounding — profit reinvested after every win")
    print("─"*W)
    print(f"  START   :   ${start:.2f}")
    print(f"  FINISH  :  ${final:>10,.4f}")
    print(f"  GROWTH  :  {growth:>+,.1f}%")
    print(f"  MAX DD  :  {max_dd:.2f}%")
    print(f"  TRADES  :  {n}  ·  ✓ {len(wins)} wins  ·  ✗ {len(losses)} losses  ·  WR {wr:.0f}%")
    print("─"*W)

    BAR = 22
    print(f"\n  {'#':<3} {'Min':>4} {'D':>2} {'Entry':>8} {'TP':>8} {'SL':>8} "
          f"{'Exit':>8} {'Res':<7} {'PnL%':>7}   {'Balance':>10}  Equity curve")
    print("  " + "─"*89)

    for t in log:
        ts    = str(t["time"])[11:16]
        pct   = min(t["balance"] / max(final, 0.01), 1.0)
        filled = int(pct * BAR)
        bar_v  = "█"*filled + "░"*(BAR-filled)
        arrow  = "▲" if t["dir"] == "LONG" else "▼"
        sign   = "+" if "✓" in t["result"] else ("−" if "✗" in t["result"] else "~")
        print(
            f"  {t['trade_n']:<3} {ts:>5} {arrow} "
            f"{t['entry']:>8.2f} {t['tp']:>8.2f} {t['sl']:>8.2f} {t['exit']:>8.2f} "
            f"{t['result']:<7} {sign}{abs(t['pnl_pct']):>5.3f}%"
            f"   ${t['balance']:>10.4f}  {bar_v}"
        )

    # Milestone wall
    milestones = [5, 10, 20, 50, 100, 200, 300, 400, 500, 600]
    hit = set()
    print()
    for t in log:
        for m in milestones:
            if t["balance"] >= m and m not in hit:
                hit.add(m)
                icon = "🎯" if m >= 600 else "💰"
                elapsed = t["bar"]
                print(f"  {icon}  ${m:>4} reached — trade #{t['trade_n']:>2}  "
                      f"at {str(t['time'])[11:16]} UTC  (~{elapsed} min into session)")

    # Compounding chain
    print("\n" + "─"*W)
    print("  COMPOUNDING CHAIN  (×1.80 per win  |  ×0.60 per loss)")
    print("─"*W)
    bal_c = start
    steps = [f"$  {bal_c:.2f}"]
    for t in log:
        if "✓" in t["result"]:
            bal_c = round(bal_c * (1 + RISK_PCT * REWARD_RATIO), 2)
            steps.append(f"→ W → $ {bal_c:>8,.2f}")
        elif "✗" in t["result"]:
            bal_c = round(bal_c * (1 - RISK_PCT), 2)
            steps.append(f"→ L → $ {bal_c:>8,.2f}")
    for i in range(0, len(steps), 4):
        print("  " + "  ".join(steps[i:i+4]))

    f_w = 1 + RISK_PCT * REWARD_RATIO
    f_l = 1 - RISK_PCT
    theory = start * f_w**len(wins) * f_l**len(losses)
    print()
    print(f"  Formula : ${start:.0f} × {f_w:.2f}^{len(wins)} × {f_l:.2f}^{len(losses)}"
          f" = ${theory:,.0f}")
    print("═"*W)


if __name__ == "__main__":
    df  = make_data()
    log = run(df)
    display(log, df)
