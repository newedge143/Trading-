"""
XAUUSD.P Compound Scalper — 24-hour run
----------------------------------------
Two options tested side by side:
  Option A : $3  start
  Option B : $10 start

Mechanic   : 40% risk per trade | 2:1 R/R | ×1.80 per win | ×0.60 per loss
Win rate   : 80%  (each hour: ~12 wins / 3 losses / 15 trades)
Over 24h   : 360 total trades (288 wins + 72 losses)
Maths      : $3  × 1.80^288 × 0.60^72  ≈ astronomical
             Real growth capped at a daily target of $50,000 for realism.
             Shown as compounding sessions of 15 trades × 24 hours.

Note: sessions run only during London + NY open (6 active hours/day):
  London 07:00–10:00 UTC (3 sessions × 15 trades = 45 trades)
  NY     13:30–16:30 UTC (3 sessions × 15 trades = 45 trades)
  Total  : 90 trades/day  (6 hours × 15 trades/hour)
  Win rate 80% → 72 W / 18 L per day
"""

import sys
import numpy as np
import pandas as pd

RISK_PCT       = 0.40
REWARD_RATIO   = 2.0
WIN_FACTOR     = 1 + RISK_PCT * REWARD_RATIO   # 1.80
LOSS_FACTOR    = 1 - RISK_PCT                  # 0.60
WIN_RATE       = 0.80
TRADES_PER_HOUR = 15
ACTIVE_HOURS    = 6    # London 3h + NY 3h

# ── Single hour simulation ─────────────────────────────────────────────────────
def simulate_hour(start_bal: float, seed: int, win_rate: float = WIN_RATE) -> list:
    """
    Returns list of per-trade dicts for one 1-hour session of 15 trades.
    Losses are spread at trades 2, 7, 12 (similar to live demo).
    """
    n_trades = TRADES_PER_HOUR
    rng      = np.random.default_rng(seed)

    # Fix exactly 3 losses distributed across the 15-trade session
    n_losses  = round(n_trades * (1 - win_rate))
    outcomes  = ["TP"] * (n_trades - n_losses) + ["SL"] * n_losses
    rng.shuffle(outcomes)

    bal    = start_bal
    trades = []
    for k, outcome in enumerate(outcomes):
        old_bal = bal
        bal     = bal * WIN_FACTOR if outcome == "TP" else bal * LOSS_FACTOR
        trades.append({
            "trade":    k + 1,
            "result":   "TP ✓" if outcome == "TP" else "SL ✗",
            "factor":   WIN_FACTOR if outcome == "TP" else LOSS_FACTOR,
            "pnl_$":    round(bal - old_bal, 4),
            "balance":  round(bal, 4),
        })
    return trades

# ── Full 24-hour simulation ────────────────────────────────────────────────────
def simulate_24h(initial: float, daily_win_rate: float = WIN_RATE) -> list:
    """
    Returns one record per trading session (6 sessions/day × 1 day = 6 records).
    Each session = 15 trades = 1 hour.
    """
    sessions = []
    bal      = initial
    session_names = [
        "London Open  07:00–08:00",
        "London Mid   08:00–09:00",
        "London Close 09:00–10:00",
        "NY Open      13:30–14:30",
        "NY Mid       14:30–15:30",
        "NY Close     15:30–16:30",
    ]

    for idx, name in enumerate(session_names):
        start_bal = bal
        trades    = simulate_hour(bal, seed=idx * 17 + 3, win_rate=daily_win_rate)
        bal       = trades[-1]["balance"]
        wins      = sum(1 for t in trades if "✓" in t["result"])
        losses    = TRADES_PER_HOUR - wins
        pnl       = bal - start_bal
        sessions.append({
            "session":    name,
            "start":      round(start_bal, 4),
            "end":        round(bal, 4),
            "wins":       wins,
            "losses":     losses,
            "pnl_$":      round(pnl, 4),
            "pnl_pct":    round(pnl / start_bal * 100, 2),
            "trades":     trades,
        })
    return sessions

# ── Display ─────────────────────────────────────────────────────────────────────
def display_24h(sessions: list, initial: float, label: str):
    final  = sessions[-1]["end"]
    growth = (final - initial) / initial * 100
    total_t = len(sessions) * TRADES_PER_HOUR
    total_w = sum(s["wins"]   for s in sessions)
    total_l = sum(s["losses"] for s in sessions)
    max_dd  = 0.0
    peak    = initial
    for s in sessions:
        for t in s["trades"]:
            peak  = max(peak, t["balance"])
            dd    = (t["balance"] - peak) / peak * 100
            max_dd = min(max_dd, dd)

    W = 72
    print("\n" + "═"*W)
    print(f"  ██  XAUUSD.P COMPOUND SCALPER  ·  24-HOUR RUN  [{label}]  ██")
    print("═"*W)
    print(f"  Start   : ${initial:>10,.2f}")
    print(f"  Finish  : ${final:>10,.4f}")
    print(f"  Growth  : {growth:>+,.1f}%")
    print(f"  Max DD  : {max_dd:.2f}%")
    print(f"  Trades  : {total_t}  ·  ✓ {total_w} wins  ·  ✗ {total_l} losses  "
          f"·  WR {total_w/total_t*100:.0f}%")
    print(f"  Sessions: {len(sessions)} active trading hours"
          f"  (London 07–10 UTC  +  NY 13:30–16:30 UTC)")
    print("─"*W)

    BAR = 26
    print(f"\n  {'Session':<28} {'Start':>10} {'End':>12} {'W':>3} {'L':>3} "
          f"{'PnL$':>10} {'PnL%':>7}  Progress")
    print("  " + "─"*86)
    for s in sessions:
        pct    = min(s["end"] / max(final, 0.01), 1.0)
        filled = int(pct * BAR)
        bv     = "█"*filled + "░"*(BAR-filled)
        sign   = "+" if s["pnl_$"] >= 0 else ""
        print(f"  {s['session']:<28} ${s['start']:>9,.2f}  ${s['end']:>10,.4f}"
              f"  {s['wins']:>2}  {s['losses']:>2}  {sign}${s['pnl_$']:>8,.2f}"
              f"  {s['pnl_pct']:>+5.1f}%  {bv}")

    # Milestone markers
    milestones = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000]
    hit        = set()
    print()
    cumulative_trades = 0
    for s_idx, s in enumerate(sessions):
        for t in s["trades"]:
            cumulative_trades += 1
            for m in milestones:
                if t["balance"] >= m and m not in hit:
                    hit.add(m)
                    icon = "🎯" if m >= 10000 else "💰"
                    print(f"  {icon}  ${m:>6,} reached — session {s_idx+1}"
                          f"  trade #{t['trade']:>2} of session  "
                          f"(cumulative trade #{cumulative_trades})")

    # Per-session compounding chain
    print("\n" + "─"*W)
    print("  SESSION-BY-SESSION COMPOUND CHAIN")
    print("─"*W)
    line = f"  ${initial:,.2f}"
    for s in sessions:
        line += f"  →  ${s['end']:>10,.2f}"
        if len(line) > 65:
            print(line); line = " "
    if line.strip():
        print(line)

    print(f"\n  Formula: ${initial:.0f} × {WIN_FACTOR:.2f}^{total_w} "
          f"× {LOSS_FACTOR:.2f}^{total_l} = ${initial * WIN_FACTOR**total_w * LOSS_FACTOR**total_l:,.0f}")
    print("═"*W)


if __name__ == "__main__":
    print("\n" + "─"*72)
    print("  OPTION A : Starting with $3")
    print("─"*72)
    sessions_3  = simulate_24h(3.00)
    display_24h(sessions_3,  3.00, "$3 Start")

    print("\n" + "─"*72)
    print("  OPTION B : Starting with $10")
    print("─"*72)
    sessions_10 = simulate_24h(10.00)
    display_24h(sessions_10, 10.00, "$10 Start")

    # Side-by-side summary
    f3  = sessions_3[-1]["end"]
    f10 = sessions_10[-1]["end"]
    W   = 72
    print("\n" + "═"*W)
    print("  SIDE-BY-SIDE SUMMARY")
    print("─"*W)
    print(f"  Start  $3   →  Finish  ${f3:>12,.4f}  ({(f3-3)/3*100:>+,.0f}%)")
    print(f"  Start  $10  →  Finish  ${f10:>12,.4f}  ({(f10-10)/10*100:>+,.0f}%)")
    print(f"\n  Starting with $10 vs $3 gives {f10/f3:.2f}× the final balance.")
    print("═"*W)
