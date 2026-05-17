#!/usr/bin/env python3
"""
Multi-Pair Trend-Pullback Live Bot — BloFin
=============================================
Trades SUI, SOL, ONDO simultaneously sharing one balance.

Architecture
------------
Per pair: own 5m + 1h candle history, own strategy instance, own open position.
Global:   max 2 concurrent positions, 5% of TOTAL balance risked per trade,
          adaptive risk tier as balance grows.

Order flow
----------
Entry  : LIMIT post-only at the signal close price (maker fee)
Exit SL: STOP-MARKET (taker — but we want guaranteed exit if SL hit)
Exit TP: LIMIT (maker)
Trailing: monitored in-bot; when triggered, cancel TP and place STOP-MARKET

Run:
  python live_bot_multi.py             # live
  python live_bot_multi.py --dry-run   # no real orders
"""

import os
import sys
import io
import time
import logging
import csv
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import ccxt
import pandas as pd
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from src.trend_pullback import TrendPullbackStrategy, Signal
from src.indicators import atr as atr_fn

# ─── CREDENTIALS ─────────────────────────────────────────────────────────────
BLOFIN_KEY        = "ab276f84c23c4a3f9eedb6cef79f4b14"
BLOFIN_SECRET     = "d6071650001642daba0b276fb4dfe96a"
BLOFIN_PASSPHRASE = "yodamoney"

# ─── TRADING CONFIG ──────────────────────────────────────────────────────────
PAIRS = ["SUI/USDT:USDT", "SOL/USDT:USDT", "ONDO/USDT:USDT"]
TIMEFRAME_5M     = "5m"
TIMEFRAME_1H     = "1h"
CANDLES_5M       = 300
CANDLES_1H       = 300
MAX_OPEN         = 2
LEVERAGE         = 20
MAX_DAILY_LOSS_PCT = 0.20
TRAIL_ACTIVATE_RR  = 1.5
TRAIL_DISTANCE_ATR = 1.5

STRATEGY_CONFIG = {
    "strategy": {
        "htf_ema_period":     50,
        "htf_slope_lookback": 5,
        "htf_slope_min_pct":  0.0005,
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
        "atr_sl_mult":       1.2,
        "reward_risk_ratio": 3.0,
    },
}

# ─── LOGGING ─────────────────────────────────────────────────────────────────
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"multi_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")],
)
log = logging.getLogger("multi")

JOURNAL = LOG_DIR / "trades_multi.csv"
if not JOURNAL.exists():
    with JOURNAL.open("w", newline="") as f:
        csv.writer(f).writerow([
            "ts", "pair", "side", "entry", "exit", "outcome",
            "qty", "net_pnl", "balance"
        ])


# ─── ADAPTIVE RISK ───────────────────────────────────────────────────────────
def adaptive_risk_pct(balance: float) -> float:
    if balance < 50:    return 0.05    # 5% — sane aggressive while small
    if balance < 100:   return 0.04
    if balance < 250:   return 0.03
    return 0.02


# ─── EXCHANGE HELPERS ────────────────────────────────────────────────────────
def make_exchange():
    return ccxt.blofin({
        "apiKey":     BLOFIN_KEY,
        "secret":     BLOFIN_SECRET,
        "password":   BLOFIN_PASSPHRASE,
        "options":    {"defaultType": "swap"},
        "enableRateLimit": True,
    })


def fetch_ohlcv(ex, symbol, tf, limit):
    raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df  = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.drop(columns=["ts"], inplace=True)
    return df.astype(float)


def get_balance(ex) -> float:
    bal = ex.fetch_balance({"type": "swap"})
    return float(bal.get("USDT", {}).get("free", 0) or 0)


def set_leverage_safe(ex, symbol, lev):
    try:
        ex.set_leverage(lev, symbol, params={"marginMode": "cross"})
    except Exception as e:
        log.warning(f"set_leverage({symbol}) failed: {e}")


# ─── POSITION SIZING ─────────────────────────────────────────────────────────
def calc_qty(balance, entry, sl, market) -> float:
    risk_pct  = adaptive_risk_pct(balance)
    risk_amt  = balance * risk_pct
    sl_dist   = abs(entry - sl)
    if sl_dist <= 0:
        return 0.0
    csz = float(market.get("contractSize") or 1.0)
    min_amt = market.get("limits", {}).get("amount", {}).get("min") or 0.0
    qty = risk_amt / sl_dist
    max_qty = (balance * LEVERAGE) / (entry * csz)
    qty = min(qty, max_qty)
    if min_amt and qty < min_amt:
        return 0.0
    return qty


# ─── PER-PAIR STATE ──────────────────────────────────────────────────────────
def init_pair_state(ex, symbol):
    market = ex.market(symbol)
    return {
        "symbol":   symbol,
        "market":   market,
        "csz":      float(market.get("contractSize") or 1.0),
        "min_amt":  market.get("limits", {}).get("amount", {}).get("min") or 0.0,
        "df_5m":    None,
        "df_1h":    None,
        "strategy": TrendPullbackStrategy(STRATEGY_CONFIG),
        "open":     None,   # dict when in trade, None otherwise
        "entry_order_id": None,
        "exit_order_id":  None,
        "stop_order_id":  None,
    }


def refresh_candles(ex, st):
    st["df_5m"] = fetch_ohlcv(ex, st["symbol"], TIMEFRAME_5M, CANDLES_5M)
    st["df_1h"] = fetch_ohlcv(ex, st["symbol"], TIMEFRAME_1H, CANDLES_1H)


def open_count(states) -> int:
    return sum(1 for s in states.values() if s["open"] is not None)


# ─── ENTRY: place limit (maker) order ────────────────────────────────────────
def try_open(ex, st, balance, dry_run):
    setup = st["strategy"].generate_signal(st["df_5m"], st["df_1h"])
    if setup is None or setup.signal == Signal.NONE:
        return None

    entry_px = setup.entry
    sl_dist  = abs(setup.entry - setup.stop_loss)
    qty      = calc_qty(balance, entry_px, setup.stop_loss, st["market"])
    if qty <= 0:
        log.info(f"[{st['symbol']}] Skip: qty too small for {st['min_amt']} min lot")
        return None

    side  = "buy" if setup.signal == Signal.LONG else "sell"
    is_long = setup.signal == Signal.LONG
    activate_at = entry_px + TRAIL_ACTIVATE_RR * sl_dist if is_long \
                  else entry_px - TRAIL_ACTIVATE_RR * sl_dist
    atr_v = float(atr_fn(st["df_5m"]["high"], st["df_5m"]["low"],
                         st["df_5m"]["close"], 14).iloc[-2])

    log.info(f"[{st['symbol']}] SIGNAL {setup.signal.value.upper()} qty={qty} "
             f"entry={entry_px} SL={setup.stop_loss} TP={setup.take_profit}")
    log.info(f"[{st['symbol']}] {setup.reason}")

    if dry_run:
        log.info(f"[{st['symbol']}] [DRY-RUN] would place limit {side} {qty} @ {entry_px}")
        order_id = f"dry-{int(time.time())}"
    else:
        try:
            order = ex.create_order(
                st["symbol"], "limit", side, qty, entry_px,
                params={"postOnly": True, "marginMode": "cross"},
            )
            order_id = order["id"]
            log.info(f"[{st['symbol']}] Placed maker LIMIT {side} {qty} @ {entry_px}  id={order_id}")
        except Exception as e:
            log.error(f"[{st['symbol']}] Order failed: {e}")
            return None

    st["entry_order_id"] = order_id
    st["open"] = {
        "side":         "LONG" if is_long else "SHORT",
        "entry_px":     entry_px,
        "qty":          qty,
        "sl":           setup.stop_loss,
        "tp":           setup.take_profit,
        "activate_at":  activate_at,
        "trailing":     False,
        "anchor":       entry_px,
        "atr_at_entry": atr_v,
        "filled":       dry_run,   # dry-run treats as filled immediately
        "open_ts":      datetime.now(timezone.utc),
    }
    return order_id


# ─── MONITOR: check fills, trailing, exits ───────────────────────────────────
def monitor_position(ex, st, balance_holder, dry_run):
    pos = st["open"]
    if pos is None:
        return

    # ── 1. Wait for entry to fill ────────────────────────────────────────────
    if not pos["filled"]:
        try:
            o = ex.fetch_order(st["entry_order_id"], st["symbol"])
            if o["status"] == "closed":
                pos["filled"] = True
                log.info(f"[{st['symbol']}] Entry filled @ {o['average'] or o['price']}")
            elif o["status"] == "canceled":
                log.info(f"[{st['symbol']}] Entry cancelled — abandoning trade")
                st["open"] = None
                st["entry_order_id"] = None
                return
            else:
                # still pending — leave it; cancel if stale (>10 min)
                age_min = (datetime.now(timezone.utc) - pos["open_ts"]).total_seconds() / 60
                if age_min > 10:
                    log.info(f"[{st['symbol']}] Cancelling stale entry order ({age_min:.1f} min)")
                    try:
                        ex.cancel_order(st["entry_order_id"], st["symbol"])
                    except Exception:
                        pass
                    st["open"] = None
                    st["entry_order_id"] = None
                return
        except Exception as e:
            log.warning(f"[{st['symbol']}] fetch_order failed: {e}")
            return

    # ── 2. Check current price vs SL/TP/trailing ─────────────────────────────
    last = st["df_5m"].iloc[-1]
    close = float(last["close"])
    high  = float(last["high"])
    low   = float(last["low"])
    atr_e = pos["atr_at_entry"]

    if pos["side"] == "LONG":
        if high >= pos["activate_at"]:
            if not pos["trailing"]:
                log.info(f"[{st['symbol']}] Trailing stop ACTIVATED at {high}")
            pos["trailing"] = True
        if pos["trailing"]:
            pos["anchor"] = max(pos["anchor"], high)
            new_sl = pos["anchor"] - TRAIL_DISTANCE_ATR * atr_e
            if new_sl > pos["sl"]:
                pos["sl"] = new_sl
                log.info(f"[{st['symbol']}] Trailing SL ratcheted to {pos['sl']:.6f}")

        # Exit check (live price-based)
        if low <= pos["sl"]:
            _close_position(ex, st, pos, pos["sl"],
                            "TP_TRAIL" if pos["trailing"] else "SL",
                            balance_holder, dry_run)
        elif not pos["trailing"] and high >= pos["tp"]:
            _close_position(ex, st, pos, pos["tp"], "TP", balance_holder, dry_run)

    else:  # SHORT
        if low <= pos["activate_at"]:
            if not pos["trailing"]:
                log.info(f"[{st['symbol']}] Trailing stop ACTIVATED at {low}")
            pos["trailing"] = True
        if pos["trailing"]:
            pos["anchor"] = min(pos["anchor"], low)
            new_sl = pos["anchor"] + TRAIL_DISTANCE_ATR * atr_e
            if new_sl < pos["sl"]:
                pos["sl"] = new_sl
                log.info(f"[{st['symbol']}] Trailing SL ratcheted to {pos['sl']:.6f}")

        if high >= pos["sl"]:
            _close_position(ex, st, pos, pos["sl"],
                            "TP_TRAIL" if pos["trailing"] else "SL",
                            balance_holder, dry_run)
        elif not pos["trailing"] and low <= pos["tp"]:
            _close_position(ex, st, pos, pos["tp"], "TP", balance_holder, dry_run)


def _close_position(ex, st, pos, exit_px, outcome, balance_holder, dry_run):
    side = "sell" if pos["side"] == "LONG" else "buy"
    qty  = pos["qty"]
    direction = 1 if pos["side"] == "LONG" else -1
    gross = qty * (exit_px - pos["entry_px"]) * direction * st["csz"]
    pos_val = qty * pos["entry_px"] * st["csz"]
    fee = pos_val * 0.0006   # taker fee for market exit (one side)
    net = gross - fee

    log.info(f"[{st['symbol']}] CLOSING {pos['side']} {qty} @ ~{exit_px}  "
             f"outcome={outcome} est_net=${net:.4f}")

    if not dry_run:
        try:
            ex.create_order(st["symbol"], "market", side, qty,
                            params={"reduceOnly": True, "marginMode": "cross"})
            log.info(f"[{st['symbol']}] Market exit submitted")
        except Exception as e:
            log.error(f"[{st['symbol']}] Exit failed: {e} — RETRY")
            try:
                ex.create_order(st["symbol"], "market", side, qty,
                                params={"reduceOnly": True})
            except Exception as e2:
                log.error(f"[{st['symbol']}] Exit retry failed: {e2}")

    # Journal
    new_bal = balance_holder["bal"] + net
    balance_holder["bal"] = new_bal
    with JOURNAL.open("a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(),
            st["symbol"], pos["side"], pos["entry_px"], exit_px,
            outcome, qty, f"{net:.4f}", f"{new_bal:.4f}",
        ])
    st["open"] = None
    st["entry_order_id"] = None


# ─── DAILY GUARD ─────────────────────────────────────────────────────────────
class DailyGuard:
    def __init__(self):
        self.day_start = None
        self.day_open_balance = None

    def check(self, balance: float) -> bool:
        now = datetime.now(timezone.utc).date()
        if self.day_start != now:
            self.day_start = now
            self.day_open_balance = balance
        loss_pct = (balance - self.day_open_balance) / self.day_open_balance
        if loss_pct <= -MAX_DAILY_LOSS_PCT:
            log.error(f"DAILY LOSS LIMIT HIT: {loss_pct*100:.1f}% — STOPPING NEW ENTRIES")
            return False
        return True


# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log.info("=" * 70)
    log.info(f"  MULTI-PAIR TREND-PULLBACK BOT  --  BloFin")
    log.info(f"  Pairs    : {PAIRS}")
    log.info(f"  Mode     : {'DRY-RUN' if args.dry_run else 'LIVE'}")
    log.info(f"  Strategy : 1h trend filter + 5m EMA20 pullback (maker fees)")
    log.info(f"  Risk     : adaptive (5%→2% by balance tier)")
    log.info(f"  Max open : {MAX_OPEN} concurrent positions")
    log.info(f"  Leverage : {LEVERAGE}x")
    log.info("=" * 70)

    ex = make_exchange()
    ex.load_markets()

    states = {}
    for p in PAIRS:
        states[p] = init_pair_state(ex, p)
        log.info(f"  Loaded {p}  min_lot={states[p]['min_amt']}  csz={states[p]['csz']}")
        if not args.dry_run:
            set_leverage_safe(ex, p, LEVERAGE)

    initial_bal = get_balance(ex)
    balance_holder = {"bal": initial_bal}
    log.info(f"  Account balance: ${initial_bal:.4f}")

    guard = DailyGuard()
    log.info("Bot running. Loop every 60s ...")

    while True:
        try:
            now = datetime.now(timezone.utc)
            balance = get_balance(ex)
            balance_holder["bal"] = balance

            allow_new = guard.check(balance)

            for sym, st in states.items():
                try:
                    refresh_candles(ex, st)
                except Exception as e:
                    log.warning(f"[{sym}] refresh failed: {e}")
                    continue

                if st["open"] is not None:
                    monitor_position(ex, st, balance_holder, args.dry_run)

            for sym, st in states.items():
                if st["open"] is not None:
                    continue
                if open_count(states) >= MAX_OPEN:
                    break
                if not allow_new:
                    break
                try:
                    try_open(ex, st, balance_holder["bal"], args.dry_run)
                except Exception as e:
                    log.warning(f"[{sym}] try_open failed: {e}")

            log.info(f"[{now.strftime('%H:%M')}] balance=${balance:.4f}  "
                     f"open={open_count(states)}/{MAX_OPEN}  "
                     f"states={ {s: ('OPEN' if st['open'] else 'flat') for s,st in states.items()} }")

            time.sleep(60)
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — shutting down")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
