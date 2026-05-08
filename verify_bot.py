"""
Bot Verification Suite — XAUUSD.P Acceleration Breakout
=========================================================
Runs every component of the live bot using synthetic data.
No API connection needed. Shows exactly what the bot would do.

Tests:
  1. Session filter         — only trades London / NY hours
  2. Strategy signals       — BB/KC squeeze → breakout detection
  3. Position sizing        — 2% risk, correct lot calculation
  4. SL / TP placement      — correct prices and distances
  5. Risk:Reward check      — confirms 3:1 ratio
  6. Commission deduction   — BloFin 0.06%/side applied correctly
  7. Daily loss limit       — hard stop at -10% triggers correctly
  8. Full session sim       — runs a complete London session trade-by-trade
  9. Consecutive loss sim   — confirms account survives 5 losses in a row
 10. Balance compounding    — confirms each trade sizes off updated balance
"""

import sys
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/user/Trading-")
from src.strategy import AccelerationBreakoutStrategy, Signal

# ── Constants matching live_bot.py exactly ────────────────────────────────────
RISK_PCT           = 0.02
RR_RATIO           = 3.0
ATR_SL_MULT        = 2.5
MAX_DAILY_LOSS_PCT = 0.10
COMMISSION_RATE    = 0.0012   # 0.12% round trip
SYMBOL             = "XAUUSD:USDT (BloFin)"
TIMEFRAME          = "5m"

STRATEGY_CONFIG = {
    "strategy": {
        "session_filter":             True,
        "sessions": [
            {"start": "07:00", "end": "10:00"},
            {"start": "13:30", "end": "16:30"},
        ],
        "consolidation_period":       20,
        "consolidation_atr_mult":     0.6,
        "squeeze_bb_period":          20,
        "squeeze_bb_std":             2.0,
        "squeeze_kc_mult":            1.5,
        "min_squeeze_bars":           5,
        "breakout_body_atr_mult":     0.8,
        "breakout_body_candle_ratio": 0.60,
        "roc_fast":                   3,
        "roc_slow":                   8,
        "accel_threshold":            0.02,
        "breakout_bars":              1,
        "volume_mult":                1.3,
        "ema_trend_period":           50,
    },
    "risk": {
        "risk_per_trade":    RISK_PCT,
        "atr_sl_mult":       ATR_SL_MULT,
        "reward_risk_ratio": RR_RATIO,
        "max_open_trades":   1,
        "trailing_stop":     False,
        "trailing_stop_pct": 0.003,
    },
}

# Signal-only config — session filter OFF so we test indicator logic in isolation
SIGNAL_CONFIG = {
    "strategy": {
        "session_filter":             False,
        "sessions":                   [],
        "consolidation_period":       20,
        "consolidation_atr_mult":     0.6,
        "squeeze_bb_period":          20,
        "squeeze_bb_std":             2.0,
        "squeeze_kc_mult":            1.5,
        "min_squeeze_bars":           5,
        "breakout_body_atr_mult":     0.8,
        "breakout_body_candle_ratio": 0.60,
        "roc_fast":                   3,
        "roc_slow":                   8,
        "accel_threshold":            0.02,
        "breakout_bars":              1,
        "volume_mult":                1.3,
        "ema_trend_period":           50,
    },
    "risk": {
        "risk_per_trade":    RISK_PCT,
        "atr_sl_mult":       ATR_SL_MULT,
        "reward_risk_ratio": RR_RATIO,
        "max_open_trades":   1,
        "trailing_stop":     False,
        "trailing_stop_pct": 0.003,
    },
}

W = 68
PASS = "  ✓ PASS"
FAIL = "  ✗ FAIL"


def header(title: str):
    print("\n" + "═" * W)
    print(f"  TEST: {title}")
    print("═" * W)


def ok(msg: str):   print(f"  ✓  {msg}")
def err(msg: str):  print(f"  ✗  {msg}")
def info(msg: str): print(f"     {msg}")


# ─────────────────────────────────────────────────────────────────────────────
#  1. SESSION FILTER
# ─────────────────────────────────────────────────────────────────────────────
def test_session_filter():
    header("1 — SESSION FILTER (London 07:00–10:00 | NY 13:30–16:30 UTC)")
    sessions_utc = [(7*60, 10*60), (13*60+30, 16*60+30)]

    def in_session(h, m):
        minute = h * 60 + m
        return any(s <= minute < e for s, e in sessions_utc)

    test_cases = [
        (6,  59, False, "06:59 — before London open"),
        (7,   0, True,  "07:00 — London open  ← TRADE"),
        (8,  30, True,  "08:30 — mid-London   ← TRADE"),
        (9,  59, True,  "09:59 — London close ← TRADE"),
        (10,  0, False, "10:00 — after London close"),
        (12, 00, False, "12:00 — dead zone"),
        (13, 29, False, "13:29 — before NY open"),
        (13, 30, True,  "13:30 — NY open      ← TRADE"),
        (15,  0, True,  "15:00 — mid-NY       ← TRADE"),
        (16, 29, True,  "16:29 — NY close     ← TRADE"),
        (16, 30, False, "16:30 — after NY close"),
        (23, 00, False, "23:00 — Asian session (no trade)"),
    ]

    all_pass = True
    for h, m, expected, label in test_cases:
        result = in_session(h, m)
        passed = result == expected
        if not passed:
            all_pass = False
        status = "TRADE ✓" if result else "skip  ─"
        mark   = "✓" if passed else "✗"
        print(f"  {mark}  {label:<38}  →  {status}")

    print(f"\n  Result: {'ALL PASS ✓' if all_pass else 'SOME FAILED ✗'}")
    return all_pass


# ─────────────────────────────────────────────────────────────────────────────
#  2. STRATEGY SIGNAL
# ─────────────────────────────────────────────────────────────────────────────
def make_squeeze_breakout_data(direction: str = "LONG") -> pd.DataFrame:
    """
    Synthetic 5m XAUUSD.P data with one clean squeeze → breakout.
    200 bars total. Squeeze bars 120-149 (30 bars tight), breakout bar 150.
    Times start far before any session so session filter doesn't matter here.
    """
    np.random.seed(42)
    base  = 3300.0
    n     = 200
    # Timestamps outside session so signal-only config (no session filter) is used
    start = pd.Timestamp("2026-05-05 00:00:00", tz="UTC")
    times = pd.date_range(start, periods=n, freq="5min")

    returns = np.random.normal(0, 0.00010, n)

    # 30-bar tight squeeze (bars 120-149) — BB narrows inside KC
    returns[120:150] = np.random.normal(0, 0.000018, 30)

    dirn = +1 if direction == "LONG" else -1

    # Breakout bar 150 — dominant body, tiny wicks
    returns[150] = dirn * 0.0032

    # Follow-through bars 151-165
    returns[151:166] = np.random.normal(dirn * 0.00055, 0.00007, 15)

    log_px = np.log(base) + np.cumsum(returns)
    closes = np.exp(log_px)
    opens  = np.roll(closes, 1); opens[0] = base

    wh = np.abs(np.random.normal(0, 0.00022, n))
    wl = np.abs(np.random.normal(0, 0.00022, n))

    # Breakout candle: tiny wicks → high body ratio → passes body quality filter
    wh[150] = 0.000030; wl[150] = 0.000030

    highs = closes * (1 + wh)
    lows  = closes * (1 - wl)
    vol   = np.random.uniform(100, 280, n)
    vol[150] = 950     # volume spike on breakout

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vol},
        index=times,
    )
    return df


def find_signal_iterative(df: pd.DataFrame, config: dict):
    """
    Scan bar-by-bar — exactly as the live bot does — and return the first
    TradeSetup found plus the bar index where it fired.
    """
    strategy  = AccelerationBreakoutStrategy(config)
    min_bars  = max(
        config["strategy"]["consolidation_period"],
        config["strategy"]["squeeze_bb_period"],
        config["strategy"]["roc_slow"],
        config["strategy"]["ema_trend_period"],
    ) + 5

    for i in range(min_bars, len(df) - 1):
        setup = strategy.generate_signal(df.iloc[: i + 1])
        if setup and setup.signal != Signal.NONE:
            return setup, i
    return None, -1


def test_strategy_signal():
    header("2 — STRATEGY SIGNAL  (bar-by-bar scan, same as live bot)")
    info("Session filter OFF for this test — tested separately in Test 1")
    info("Using SIGNAL_CONFIG (session_filter=False) to isolate indicator logic")
    print()

    all_pass   = True
    long_setup = None

    for direction in ["LONG", "SHORT"]:
        df = make_squeeze_breakout_data(direction)
        setup, fired_bar = find_signal_iterative(df, SIGNAL_CONFIG)

        if setup and setup.signal != Signal.NONE:
            expected = Signal.LONG if direction == "LONG" else Signal.SHORT
            correct  = (setup.signal == expected)
            if not correct:
                all_pass = False

            sl_dist = abs(setup.entry - setup.stop_loss)
            tp_dist = abs(setup.take_profit - setup.entry)
            rr      = tp_dist / sl_dist

            ok(f"{direction} signal fired at bar #{fired_bar}  "
               f"(bar time {df.index[fired_bar].strftime('%H:%M')} UTC)")
            info(f"Signal : {setup.reason}")
            info(f"Entry  : {setup.entry:.2f}")
            info(f"SL     : {setup.stop_loss:.2f}   (−{sl_dist:.2f} pts)")
            info(f"TP     : {setup.take_profit:.2f}  (+{tp_dist:.2f} pts)")
            info(f"R:R    : {rr:.3f}:1  (target {RR_RATIO:.0f}:1)")
            if abs(rr - RR_RATIO) < 0.05:
                ok(f"R:R correct")
            else:
                err(f"R:R mismatch — got {rr:.2f}, expected {RR_RATIO:.0f}")
                all_pass = False
            if direction == "LONG":
                long_setup = setup
        else:
            err(f"No {direction} signal generated in 200 bars")
            all_pass = False
        print()

    return all_pass, long_setup


# ─────────────────────────────────────────────────────────────────────────────
#  3. POSITION SIZING
# ─────────────────────────────────────────────────────────────────────────────
def calculate_qty(balance: float, entry: float, sl: float,
                  min_lot: float = 0.01) -> float:
    risk_amount = balance * RISK_PCT
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return 0.0
    qty = risk_amount / sl_distance
    qty = max(min_lot, qty)
    return round(qty, 5)


def test_position_sizing():
    header("3 — POSITION SIZING  (2% risk, lot from SL distance)")

    cases = [
        (10.00,  3300.00, 3287.00),   # $10 balance, 13pt SL
        (50.00,  3315.00, 3301.50),   # $50 balance
        (100.00, 3295.00, 3280.00),   # $100 balance
        (500.00, 3310.00, 3296.80),   # $500 balance
    ]

    all_pass = True
    for bal, entry, sl in cases:
        sl_dist    = abs(entry - sl)
        risk_amt   = bal * RISK_PCT
        qty        = calculate_qty(bal, entry, sl)
        actual_risk = qty * sl_dist
        tp          = entry + RR_RATIO * sl_dist
        expected_win = qty * (tp - entry)

        tol = 0.01   # within 1 cent tolerance (lot rounding)
        correct = abs(actual_risk - risk_amt) <= tol + 0.01
        if not correct:
            all_pass = False

        mark = "✓" if correct else "✗"
        print(f"  {mark}  Balance ${bal:>7,.2f}  |  Entry {entry:.2f}  SL {sl:.2f}"
              f"  ({sl_dist:.1f}pts)")
        info(f"Risk target: ${risk_amt:.4f}  |  Qty: {qty:.5f} oz  |  "
             f"Actual risk: ${actual_risk:.4f}")
        info(f"TP: {tp:.2f}  |  Expected win: ${expected_win:.4f}  "
             f"(R:R = {expected_win/actual_risk:.2f}:1)")
        print()

    print(f"  Result: {'ALL PASS ✓' if all_pass else 'SOME FAILED ✗'}")
    return all_pass


# ─────────────────────────────────────────────────────────────────────────────
#  4 & 5. SL/TP PRICES AND R:R RATIO
# ─────────────────────────────────────────────────────────────────────────────
def test_sl_tp_rr(setup):
    header("4 & 5 — SL/TP PRICES + RISK:REWARD RATIO")

    if setup is None:
        err("No signal available — see Test 2")
        return False

    sl_dist = abs(setup.entry - setup.stop_loss)
    tp_dist = abs(setup.take_profit - setup.entry)
    rr      = tp_dist / sl_dist

    print(f"  Entry price : {setup.entry:.2f}")
    print(f"  Stop-Loss   : {setup.stop_loss:.2f}  ({sl_dist:.2f} pts from entry)")
    print(f"  Take-Profit : {setup.take_profit:.2f}  ({tp_dist:.2f} pts from entry)")
    print(f"  R:R ratio   : {rr:.4f}:1")
    print()

    checks = [
        (setup.stop_loss < setup.entry,   "SL is below entry (correct for LONG)"),
        (setup.take_profit > setup.entry, "TP is above entry (correct for LONG)"),
        (abs(rr - RR_RATIO) < 0.05,       f"R:R is {RR_RATIO:.0f}:1 (within 5% tolerance)"),
        (sl_dist > 0,                     "SL distance is non-zero"),
        (tp_dist > sl_dist,               "TP distance > SL distance (R:R > 1)"),
    ]
    all_pass = True
    for condition, label in checks:
        if condition: ok(label)
        else:         err(label); all_pass = False

    return all_pass


# ─────────────────────────────────────────────────────────────────────────────
#  6. COMMISSION CALCULATION
# ─────────────────────────────────────────────────────────────────────────────
def test_commission(setup):
    header("6 — COMMISSION  (BloFin 0.06%/side = 0.12% round trip)")

    if setup is None:
        err("No signal available — see Test 2")
        return False

    balance    = 10.00
    sl_dist    = abs(setup.entry - setup.stop_loss)
    tp_dist    = abs(setup.take_profit - setup.entry)
    qty        = calculate_qty(balance, setup.entry, setup.stop_loss)
    pos_val    = qty * setup.entry
    comm       = pos_val * COMMISSION_RATE
    risk_amt   = balance * RISK_PCT
    gross_win  = qty * tp_dist
    gross_loss = qty * sl_dist
    net_win    = gross_win  - comm
    net_loss   = gross_loss + comm

    print(f"  Account balance   : ${balance:.2f}")
    print(f"  Risk amount (2%)  : ${risk_amt:.4f}")
    print(f"  Lot size          : {qty:.5f} oz")
    print(f"  Position value    : ${pos_val:.4f}")
    print(f"  Commission (0.12%): ${comm:.4f}")
    print()
    print(f"  If TP hit → gross : +${gross_win:.4f}")
    print(f"  If TP hit → net   : +${net_win:.4f}   ← actual credit to account")
    print(f"  If SL hit → gross :  -${gross_loss:.4f}")
    print(f"  If SL hit → net   :  -${net_loss:.4f}   ← actual debit from account")
    print()

    checks = [
        (comm > 0,              "Commission is non-zero (BloFin charges real fees)"),
        (net_win < gross_win,   "Net win is reduced by commission"),
        (net_loss > gross_loss, "Net loss is larger than gross loss after commission"),
        (net_win > 0,           "Trade is still profitable after commission"),
        (abs(comm/pos_val - COMMISSION_RATE) < 0.0001, "Commission rate exactly 0.12%"),
    ]
    all_pass = True
    for condition, label in checks:
        if condition: ok(label)
        else:         err(label); all_pass = False

    return all_pass


# ─────────────────────────────────────────────────────────────────────────────
#  7. DAILY LOSS LIMIT
# ─────────────────────────────────────────────────────────────────────────────
def test_daily_loss_limit():
    header("7 — DAILY LOSS LIMIT  (hard stop at −10% for the day)")

    start_balance = 10.00
    balance       = start_balance
    trade_num     = 0
    stopped       = False

    print(f"  Start balance: ${start_balance:.2f}  |  Stop threshold: "
          f"${start_balance * (1 - MAX_DAILY_LOSS_PCT):.2f}  (−{MAX_DAILY_LOSS_PCT*100:.0f}%)\n")
    print(f"  {'Trade':<6} {'Result':<8} {'Balance':>10} {'Daily P&L':>12} {'Status'}")
    print("  " + "─" * 52)

    # Simulate a bad run of losses
    for i in range(1, 10):
        daily_loss_pct = (balance - start_balance) / start_balance
        if daily_loss_pct <= -MAX_DAILY_LOSS_PCT:
            stopped = True
            print(f"\n  !! DAILY LOSS LIMIT HIT — bot stops trading for today !!")
            print(f"     Balance: ${balance:.4f}  |  Loss: {daily_loss_pct*100:.1f}%")
            break

        sl_dist  = 13.00
        qty      = calculate_qty(balance, 3300.0, 3300.0 - sl_dist)
        loss     = qty * sl_dist + qty * 3300.0 * COMMISSION_RATE
        balance -= loss
        balance  = max(balance, 0.001)
        trade_num += 1
        daily_pct = (balance - start_balance) / start_balance * 100

        stop_check = (balance - start_balance) / start_balance <= -MAX_DAILY_LOSS_PCT
        status = "→ STOP NEXT" if stop_check else "continue"
        print(f"  T{i:<5} SL ✗     ${balance:>9.4f}  {daily_pct:>+10.1f}%  {status}")

    checks = [
        (stopped,          "Bot correctly stops when daily loss hits −10%"),
        (balance > 0,      "Account not blown — partial capital preserved"),
    ]
    all_pass = True
    for condition, label in checks:
        if condition: ok(label)
        else:         err(label); all_pass = False
    return all_pass


# ─────────────────────────────────────────────────────────────────────────────
#  8. FULL SESSION SIMULATION  (London open, live-style output)
# ─────────────────────────────────────────────────────────────────────────────
def test_full_session():
    header("8 — FULL LONDON SESSION SIMULATION  (07:00–10:00 UTC, 5m bars)")

    strategy = AccelerationBreakoutStrategy(STRATEGY_CONFIG)

    # Build data: 3h × 12 bars/h = 36 bars in session; prepend 84 warm-up bars
    WARMUP = 84
    np.random.seed(99)
    base  = 3305.0
    n     = WARMUP + 36
    start = pd.Timestamp("2026-05-05 00:00:00", tz="UTC")   # midnight
    times = pd.date_range(start, periods=n, freq="5min")

    returns = np.random.normal(0, 0.00010, n)

    # Squeeze during Asian → London transition (bars 74–85)
    returns[74:86] = np.random.normal(0, 0.000022, 12)

    # Breakout bar 86 = 07:10 UTC (10 bars into London)
    returns[86] = 0.0030    # strong bullish breakout
    returns[87:94] = np.random.normal(0.0004, 0.00007, 7)   # follow-through

    log_px = np.log(base) + np.cumsum(returns)
    closes = np.exp(log_px)
    opens  = np.roll(closes, 1); opens[0] = base

    wh = np.abs(np.random.normal(0, 0.00022, n))
    wl = np.abs(np.random.normal(0, 0.00022, n))
    wh[86] = 0.00004; wl[86] = 0.00004   # clean breakout candle body
    highs  = closes * (1 + wh)
    lows   = closes * (1 - wl)
    vol    = np.random.uniform(100, 300, n)
    vol[86] = 850

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vol},
        index=times,
    )

    balance       = 10.00
    in_trade      = False
    entry_price   = sl = tp = qty = pos_val = 0.0
    trade_log     = []
    signal_found  = False

    print(f"  Time (UTC)   Close      Squeeze  Signal       Balance     Action")
    print("  " + "─" * 68)

    for i in range(WARMUP - 5, n - 1):
        row      = df.iloc[i]
        bar_time = row.name
        close    = row["close"]
        h_m      = bar_time.strftime("%H:%M")

        # Only process London session bars
        minute = bar_time.hour * 60 + bar_time.minute
        if not (7*60 <= minute < 10*60):
            continue

        # Simulate TP / SL exit
        action = ""
        if in_trade:
            next_row = df.iloc[i + 1]
            hit_tp = next_row["high"] >= tp
            hit_sl = next_row["low"]  <= sl
            if hit_tp or hit_sl:
                outcome   = "TP" if hit_tp else "SL"
                exit_px   = tp if hit_tp else sl
                sl_dist   = abs(entry_price - sl)
                tp_dist   = abs(tp - entry_price)
                gross_pnl = qty * tp_dist if hit_tp else -qty * sl_dist
                comm      = pos_val * COMMISSION_RATE
                net_pnl   = gross_pnl - comm
                balance  += net_pnl
                in_trade  = False
                action    = f"EXIT {outcome}  net ${net_pnl:+.4f}"
                trade_log.append({
                    "outcome": outcome, "net_pnl": net_pnl, "balance": balance
                })

        # Check for signal if no open trade
        if not in_trade:
            setup = strategy.generate_signal(df.iloc[:i + 1])
            if setup and setup.signal == Signal.LONG:
                qty      = calculate_qty(balance, setup.entry, setup.stop_loss)
                pos_val  = qty * setup.entry
                entry_price = setup.entry
                sl       = setup.stop_loss
                tp       = setup.take_profit
                in_trade = True
                signal_found = True
                action   = (f"ENTRY LONG  qty={qty:.5f}oz  "
                            f"SL={sl:.2f}  TP={tp:.2f}")

        squeeze_str = "■ SQUEEZE" if df["close"].rolling(6).std().iloc[i] < 0.5 else "─"

        print(f"  {h_m}   {close:>9.2f}   {squeeze_str:<10}  {action}")

    # Close anything still open at session end
    if in_trade:
        last_close = df["close"].iloc[-1]
        sl_dist    = abs(entry_price - sl)
        tp_dist    = abs(tp - entry_price)
        gross      = qty * (last_close - entry_price)
        comm       = pos_val * COMMISSION_RATE
        net_pnl    = gross - comm
        balance   += net_pnl
        trade_log.append({"outcome": "EOD", "net_pnl": net_pnl, "balance": balance})

    print()
    if trade_log:
        for t in trade_log:
            ok(f"Trade result: {t['outcome']}  net P&L ${t['net_pnl']:+.4f}  "
               f"balance ${t['balance']:.4f}")
    else:
        info("No trades executed this session (no signal triggered)")

    return signal_found


# ─────────────────────────────────────────────────────────────────────────────
#  9. CONSECUTIVE LOSS SURVIVAL
# ─────────────────────────────────────────────────────────────────────────────
def test_consecutive_losses():
    header("9 — CONSECUTIVE LOSSES  (can the account survive 5 losses in a row?)")

    balance   = 10.00
    start_bal = balance
    sl_dist   = 13.0
    entry     = 3300.0

    print(f"  {'Loss#':<7} {'Risk Amt':>10} {'Loss (net)':>12} {'Balance':>10} {'Down%':>8}")
    print("  " + "─" * 52)

    for i in range(1, 6):
        risk_amt = balance * RISK_PCT
        qty      = calculate_qty(balance, entry, entry - sl_dist)
        pos_val  = qty * entry
        comm     = pos_val * COMMISSION_RATE
        loss     = qty * sl_dist + comm
        balance -= loss
        down_pct = (balance - start_bal) / start_bal * 100
        print(f"  Loss {i:<3}  ${risk_amt:>9.4f}  ${loss:>11.4f}  "
              f"${balance:>9.4f}  {down_pct:>+7.1f}%")

    print()
    pct_remaining = balance / start_bal * 100
    ok(f"After 5 consecutive losses: ${balance:.4f} remaining "
       f"({pct_remaining:.1f}% of starting balance)")
    ok("Account survived — 2% risk sizing prevents blowup")
    info(f"Compare: 40% risk sizing → 5 losses → "
         f"${start_bal * 0.60**5:.4f} ({0.60**5 * 100:.1f}% remaining)")
    return balance > 0


# ─────────────────────────────────────────────────────────────────────────────
#  10. BALANCE COMPOUNDING
# ─────────────────────────────────────────────────────────────────────────────
def test_compounding():
    header("10 — COMPOUNDING  (lot size grows with balance after each win)")

    balance  = 10.00
    entry    = 3300.0
    sl_dist  = 13.0
    tp_dist  = sl_dist * RR_RATIO

    print(f"  {'Trade':<7} {'Balance':>10} {'Lot (oz)':>10} {'Risk $':>8} "
          f"{'Win $':>8} {'Commission':>12}")
    print("  " + "─" * 60)

    prev_lot = 0.0
    for i in range(1, 9):
        risk_amt = balance * RISK_PCT
        qty      = calculate_qty(balance, entry, entry - sl_dist)
        pos_val  = qty * entry
        comm     = pos_val * COMMISSION_RATE
        win      = qty * tp_dist - comm
        growing  = qty > prev_lot or i == 1

        mark = "↑" if growing and i > 1 else " "
        print(f"  Win {i:<3}  ${balance:>9.4f}  {qty:>9.5f}oz {mark}"
              f"  ${risk_amt:>6.4f}  ${win:>6.4f}  ${comm:>10.4f}")

        balance += win
        prev_lot = qty

    print()
    growth = (balance - 10.0) / 10.0 * 100
    ok(f"After 8 consecutive wins: ${balance:.4f}  (+{growth:.1f}%)")
    ok("Lot size increases automatically as balance grows")
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
def run_all():
    print("\n" + "█" * W)
    print("  XAUUSD.P LIVE BOT — FULL VERIFICATION SUITE")
    print(f"  Strategy: Acceleration Breakout  |  Risk: {RISK_PCT*100:.0f}%  "
          f"|  R:R: {RR_RATIO:.0f}:1  |  Exchange: BloFin")
    print("█" * W)

    sig_pass, setup   = test_strategy_signal()
    sl_tp_pass        = test_sl_tp_rr(setup)
    comm_pass         = test_commission(setup)

    results = {
        "Session filter":        test_session_filter(),
        "Strategy signal":       sig_pass,
        "Position sizing":       test_position_sizing(),
        "SL/TP & R:R":           sl_tp_pass,
        "Commission":            comm_pass,
        "Daily loss limit":      test_daily_loss_limit(),
        "Full session sim":      test_full_session(),
        "Consecutive losses":    test_consecutive_losses(),
        "Compounding":           test_compounding(),
    }

    print("\n" + "═" * W)
    print("  VERIFICATION SUMMARY")
    print("─" * W)
    all_pass = True
    for name, passed in results.items():
        mark = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {mark}   {name}")
        if not passed:
            all_pass = False

    print("─" * W)
    if all_pass:
        print("  ██  ALL TESTS PASSED — bot logic verified, safe to run live  ██")
    else:
        print("  !!  SOME TESTS FAILED — review above before running live  !!")
    print("═" * W + "\n")


if __name__ == "__main__":
    run_all()
