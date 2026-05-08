#!/usr/bin/env python3
"""
XAUUSD.P Live Trading Bot — BloFin
====================================
Strategy  : Acceleration Breakout (BB/KC squeeze + momentum)
Sessions  : London 07:00–10:00 UTC  |  NY 13:30–16:30 UTC
Risk      : 2% per trade  |  3:1 R:R  |  10% daily hard stop
Exchange  : BloFin  —  XAUUSDT perpetual swap
Timeframe : 5m

Run on YOUR OWN machine:
  pip install ccxt pandas numpy
  python live_bot.py

  Add --dry-run to test without placing real orders:
  python live_bot.py --dry-run

!! SECURITY !!
  Rotate your API keys on BloFin after this setup.
  Never share credentials in plain text again.
  Ensure your API key has TRADE permission ONLY — no withdrawal.
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
import numpy as np
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Force UTF-8 output on Windows so Unicode log chars don't crash PowerShell
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from src.strategy import AccelerationBreakoutStrategy, Signal

# ─────────────────────────────────────────────────────────────────────────────
#  CREDENTIALS  —  rotate these keys on BloFin immediately after first run
# ─────────────────────────────────────────────────────────────────────────────
BLOFIN_KEY        = "ab276f84c23c4a3f9eedb6cef79f4b14"
BLOFIN_SECRET     = "d6071650001642daba0b276fb4dfe96a"
BLOFIN_PASSPHRASE = "yodamoney"

# ─────────────────────────────────────────────────────────────────────────────
#  TRADING CONFIG  —  adjust these if needed
# ─────────────────────────────────────────────────────────────────────────────
SYMBOL             = os.environ.get("TRADING_PAIR", "DOGE/USDT:USDT")  # default DOGE; override with TRADING_PAIR env var
TIMEFRAME          = "1m"
RISK_PCT           = 0.02             # 2% of balance risked per trade
RR_RATIO           = 3.0             # take profit = 3 × stop-loss distance
ATR_SL_MULT        = 2.5             # stop = 2.5 × ATR(14) below/above entry
MAX_DAILY_LOSS_PCT = 0.10            # stop all trading if down 10% on the day
LEVERAGE           = 10              # set leverage on BloFin (10× = safe start)
BAR_SECONDS        = 60              # 1 minute per candle
CANDLES_NEEDED     = 200             # history for indicators (~3.3h on 1m)

STRATEGY_CONFIG = {
    "strategy": {
        "session_filter": False,          # 24/7 — no session restrictions
        "sessions":       [],
        "consolidation_period":      10,   # 10-bar rolling high/low for breakout level
        "consolidation_atr_mult":    1.0,
        "squeeze_bb_period":         20,
        "squeeze_bb_std":            2.0,
        "squeeze_kc_mult":           1.5,
        "min_squeeze_bars":          2,
        "require_squeeze":           False, # fire on ANY momentum breakout, not just squeeze
        "breakout_body_atr_mult":    0.3,  # small body ok
        "breakout_body_candle_ratio":0.40, # 40% body ratio
        "roc_fast":                  3,
        "roc_slow":                  8,
        "accel_threshold":           0.005, # very sensitive momentum
        "breakout_bars":             1,
        "volume_mult":               1.0,  # no volume gate
        "ema_trend_period":          50,
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

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("XAUBot")

# ─────────────────────────────────────────────────────────────────────────────
#  TRADE JOURNAL  (CSV, one row per closed trade)
# ─────────────────────────────────────────────────────────────────────────────
JOURNAL_FILE = LOG_DIR / "trades.csv"
JOURNAL_HEADERS = [
    "date", "session", "direction", "entry", "sl", "tp",
    "exit_price", "outcome", "qty", "gross_pnl", "commission", "net_pnl",
    "balance_before", "balance_after", "signal_reason",
]

def journal_write(row: dict):
    exists = JOURNAL_FILE.exists()
    with open(JOURNAL_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_HEADERS)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in JOURNAL_HEADERS})


# ─────────────────────────────────────────────────────────────────────────────
#  EXCHANGE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def detect_symbol(ex: ccxt.blofin, requested: str) -> str:
    """Resolve a requested symbol (e.g. DOGE/USDT:USDT or DOGE) to BloFin's actual market."""
    markets = ex.markets

    # Direct hit
    if requested in markets:
        log.info(f"Symbol found: {requested}")
        return requested

    # Try common ccxt format variations
    base = requested.split("/")[0].split("-")[0].split(":")[0].upper()
    candidates = [
        f"{base}/USDT:USDT",
        f"{base}USDT:USDT",
        f"{base}/USDT",
        f"{base}USDT",
    ]
    for c in candidates:
        if c in markets:
            log.info(f"Symbol found: {c} (resolved from {requested})")
            return c

    # Fallback: scan by base asset
    for sym, mkt in markets.items():
        if (mkt.get("base") or "").upper() == base and "swap" in (mkt.get("type") or "").lower():
            log.info(f"Symbol found via scan: {sym}")
            return sym

    raise RuntimeError(
        f"Cannot find {base} perpetual swap on BloFin. "
        f"Available {base} markets: {[s for s in markets if base in s.upper()]}"
    )


def make_exchange() -> ccxt.blofin:
    global SYMBOL
    ex = ccxt.blofin({
        "apiKey":   BLOFIN_KEY,
        "secret":   BLOFIN_SECRET,
        "password": BLOFIN_PASSPHRASE,
        "options":  {"defaultType": "swap"},
    })
    ex.load_markets()
    SYMBOL = detect_symbol(ex, SYMBOL)
    return ex


def get_balance(ex: ccxt.blofin) -> float:
    bal = ex.fetch_balance({"type": "swap"})
    usdt = bal.get("USDT", {}).get("free", 0.0)
    return float(usdt)


def get_position(ex: ccxt.blofin) -> dict | None:
    """Return open position dict or None."""
    positions = ex.fetch_positions([SYMBOL])
    for p in positions:
        if abs(float(p.get("contracts", 0) or 0)) > 0:
            return p
    return None


def fetch_candles(ex: ccxt.blofin, limit: int = CANDLES_NEEDED) -> pd.DataFrame:
    raw = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=limit)
    df  = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.drop(columns=["ts"], inplace=True)
    return df.astype(float)


def set_leverage(ex: ccxt.blofin, lev: int):
    try:
        ex.set_leverage(lev, SYMBOL)
        log.info(f"Leverage set to {lev}x")
    except Exception as e:
        log.warning(f"Could not set leverage (may already be set): {e}")


def place_entry(ex: ccxt.blofin, side: str, qty: float,
                sl: float, tp: float, dry_run: bool) -> str | None:
    """Place market entry + SL + TP orders. Returns order ID."""
    log.info(f"{'[DRY RUN] ' if dry_run else ''}ENTRY {side.upper()} "
             f"qty={qty:.5f}  SL={sl:.2f}  TP={tp:.2f}")
    if dry_run:
        return "DRY_RUN_ORDER"
    try:
        # Market entry
        order = ex.create_market_order(
            symbol=SYMBOL,
            side=side,
            amount=qty,
            params={"reduceOnly": False},
        )
        order_id = order["id"]
        log.info(f"Entry order filled  id={order_id}")

        exit_side = "sell" if side == "buy" else "buy"

        # Take-profit limit order (reduce-only)
        ex.create_order(
            symbol=SYMBOL,
            type="limit",
            side=exit_side,
            amount=qty,
            price=tp,
            params={"reduceOnly": True, "clOrdId": f"TP_{order_id[:8]}"},
        )
        log.info(f"TP limit placed at {tp:.2f}")

        # Stop-loss stop-market order (reduce-only)
        ex.create_order(
            symbol=SYMBOL,
            type="stop_market",
            side=exit_side,
            amount=qty,
            price=sl,
            params={"stopPrice": sl, "reduceOnly": True,
                    "clOrdId": f"SL_{order_id[:8]}"},
        )
        log.info(f"SL stop placed at  {sl:.2f}")

        return order_id

    except Exception as e:
        log.error(f"Order placement failed: {e}")
        return None


def cancel_all_open_orders(ex: ccxt.blofin, dry_run: bool):
    if dry_run:
        return
    try:
        ex.cancel_all_orders(SYMBOL)
        log.info("All pending orders cancelled")
    except Exception as e:
        log.warning(f"Cancel orders error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  POSITION SIZING
# ─────────────────────────────────────────────────────────────────────────────
def calculate_qty(balance: float, entry: float, sl: float, ex: ccxt.blofin) -> float:
    """
    Position sizing with hard safety:
      1. Risk exactly RISK_PCT of balance per trade.
      2. If the resulting qty is below the exchange minimum lot, SKIP the trade
         (return 0). Forcing a min-lot position when the account is small =
         hidden over-leverage and can blow the account in one trade.
      3. If the resulting qty exceeds what the account can margin at LEVERAGE,
         skip the trade (over-leverage).
    """
    risk_amount = balance * RISK_PCT
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return 0.0
    qty = risk_amount / sl_distance

    market  = ex.market(SYMBOL)
    contract_size = float(market.get("contractSize") or 1.0)
    min_qty = market.get("limits", {}).get("amount", {}).get("min") or 0.0
    max_qty = market.get("limits", {}).get("amount", {}).get("max") or 1e9

    # Cap at available margin (prevent over-leverage even if exchange allows)
    max_notional = balance * LEVERAGE
    max_qty_by_margin = max_notional / (entry * contract_size)
    qty = min(qty, max_qty_by_margin, max_qty)

    if min_qty and qty < min_qty:
        log.warning(
            f"Skipping trade: qty {qty:.6f} below min lot {min_qty} for {SYMBOL}. "
            f"Balance ${balance:.4f} too small for 2% risk on this pair."
        )
        return 0.0

    qty = float(ex.amount_to_precision(SYMBOL, qty))
    return qty


# ─────────────────────────────────────────────────────────────────────────────
#  SESSION CHECK
# ─────────────────────────────────────────────────────────────────────────────
def in_active_session() -> bool:
    now     = datetime.now(timezone.utc)
    minute  = now.hour * 60 + now.minute
    sessions = [(7*60, 10*60), (13*60+30, 16*60+30)]
    return any(s <= minute < e for s, e in sessions)


def session_name() -> str:
    now    = datetime.now(timezone.utc)
    minute = now.hour * 60 + now.minute
    if 7*60 <= minute < 10*60:
        return "London"
    if 13*60+30 <= minute < 16*60+30:
        return "NY"
    if 22*60 <= minute or minute < 7*60:
        return "Asia"
    return "Mid"


# ─────────────────────────────────────────────────────────────────────────────
#  WAIT UNTIL NEXT BAR CLOSE
# ─────────────────────────────────────────────────────────────────────────────
def seconds_to_next_bar() -> float:
    now     = datetime.now(timezone.utc)
    elapsed = now.second                  # 1m bar: just count seconds within the minute
    wait    = BAR_SECONDS - elapsed + 2   # +2s buffer after bar closes
    return max(wait, 1)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN BOT LOOP
# ─────────────────────────────────────────────────────────────────────────────
def run(dry_run: bool = False):
    log.info("=" * 60)
    log.info("  ACCELERATION BREAKOUT BOT - BloFin LIVE")
    log.info(f"  Pair     : {SYMBOL}")
    log.info(f"  Mode     : {'** DRY RUN - no real orders **' if dry_run else 'LIVE TRADING'}")
    log.info(f"  Schedule : 24/7 - no session restrictions")
    log.info(f"  Risk     : {RISK_PCT*100:.0f}% per trade  |  R:R {RR_RATIO:.0f}:1")
    log.info(f"  Daily stop: {MAX_DAILY_LOSS_PCT*100:.0f}% max loss")
    log.info("=" * 60)

    strategy = AccelerationBreakoutStrategy(STRATEGY_CONFIG)

    # ── Connect ────────────────────────────────────────────────────────────
    log.info("Connecting to BloFin ...")
    ex = make_exchange()
    log.info("Connected  [OK]")

    balance = get_balance(ex)
    log.info(f"Account balance : ${balance:.4f} USDT")

    if balance < 1.0 and not dry_run:
        log.error("Balance below $1 - please deposit funds and restart.")
        return

    set_leverage(ex, LEVERAGE)

    day_start_balance  = balance
    open_order_id      = None
    open_side          = None
    open_entry         = None
    open_sl            = None
    open_tp            = None
    open_qty           = None
    balance_pre_trade  = None   # balance snapshot just before entry (for TP/SL detection)
    signal_reason      = ""

    log.info("Bot running. Waiting for next 1m bar ...\n")

    while True:
        try:
            # ── Daily loss hard stop ───────────────────────────────────────
            balance = get_balance(ex)
            daily_loss = (balance - day_start_balance) / day_start_balance
            if daily_loss <= -MAX_DAILY_LOSS_PCT:
                log.warning(
                    f"DAILY LOSS LIMIT HIT - down {daily_loss*100:.1f}% today. "
                    f"Stopping all trading. Restart tomorrow."
                )
                cancel_all_open_orders(ex, dry_run)
                break

            now_utc = datetime.now(timezone.utc)

            # ── Reset daily balance at midnight UTC ────────────────────────
            if now_utc.hour == 0 and now_utc.minute < 6:
                day_start_balance = balance
                log.info(f"New trading day - reset start balance to ${balance:.4f}")

            # ── Monitor open position ─────────────────────────────────────
            position = get_position(ex)

            if position is not None:
                pnl     = float(position.get("unrealizedPnl", 0) or 0)
                mark    = float(position.get("markPrice", 0) or 0)
                log.info(
                    f"[POSITION OPEN]  {open_side}  mark={mark:.2f}  "
                    f"uPnL=${pnl:+.4f}  SL={open_sl:.2f}  TP={open_tp:.2f}"
                )

                # Check if TP or SL was hit (position closed automatically)
            else:
                if open_order_id is not None:
                    # Position just closed — cancel the remaining SL or TP order
                    cancel_all_open_orders(ex, dry_run)
                    balance = get_balance(ex)
                    # TP hit if balance rose vs pre-trade snapshot, SL hit if it fell
                    outcome = "TP [WIN]" if (balance_pre_trade and balance > balance_pre_trade) else "SL [LOSS]"
                    trade_pnl = balance - balance_pre_trade if balance_pre_trade else 0
                    log.info(
                        f"TRADE CLOSED  {outcome}  "
                        f"trade P&L ${trade_pnl:+.4f}  "
                        f"balance=${balance:.4f}  "
                        f"day P&L ${balance - day_start_balance:+.4f}"
                    )
                    journal_write({
                        "date":           datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        "session":        session_name(),
                        "direction":      open_side,
                        "entry":          round(open_entry, 2) if open_entry else "",
                        "sl":             round(open_sl, 2) if open_sl else "",
                        "tp":             round(open_tp, 2) if open_tp else "",
                        "exit_price":     "",
                        "outcome":        outcome,
                        "qty":            round(open_qty, 5) if open_qty else "",
                        "net_pnl":        round(trade_pnl, 4),
                        "balance_before": round(balance_pre_trade, 4) if balance_pre_trade else "",
                        "balance_after":  round(balance, 4),
                        "signal_reason":  signal_reason,
                    })
                    open_order_id     = None
                    open_side         = None
                    open_entry        = None
                    open_sl           = None
                    open_tp           = None
                    open_qty          = None
                    balance_pre_trade = None

            # ── Look for new signal 24/7 (no session gate) ────────────────
            if open_order_id is None:
                df = fetch_candles(ex)
                if df is not None and len(df) >= 60:
                    setup = strategy.generate_signal(df)
                    if setup and setup.signal != Signal.NONE:
                        balance = get_balance(ex)
                        qty     = calculate_qty(balance, setup.entry,
                                                setup.stop_loss, ex)
                        if qty > 0:
                            side = "buy" if setup.signal == Signal.LONG else "sell"
                            pre  = get_balance(ex)
                            oid  = place_entry(
                                ex, side, qty,
                                setup.stop_loss, setup.take_profit, dry_run
                            )
                            if oid:
                                open_order_id     = oid
                                open_side         = side
                                open_entry        = setup.entry
                                open_sl           = setup.stop_loss
                                open_tp           = setup.take_profit
                                open_qty          = qty
                                balance_pre_trade = pre
                                signal_reason     = setup.reason
                                log.info(
                                    f"Signal: {setup.reason}\n"
                                    f"  entry={setup.entry:.2f}  "
                                    f"SL={setup.stop_loss:.2f}  "
                                    f"TP={setup.take_profit:.2f}  qty={qty:.5f}"
                                )
                    else:
                        log.info(
                            f"[{session_name():6}] "
                            f"{now_utc.strftime('%H:%M')} UTC  "
                            f"No signal  |  balance=${balance:.4f}"
                        )
                else:
                    log.info("Waiting for enough candle history ...")

            # ── Wait for next bar ─────────────────────────────────────────
            wait = seconds_to_next_bar()
            log.info(f"Next bar check in {wait:.0f}s ...\n")
            time.sleep(wait)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            cancel_all_open_orders(ex, dry_run)
            break

        except ccxt.NetworkError as e:
            log.warning(f"Network error - retrying in 30s: {e}")
            time.sleep(30)

        except ccxt.ExchangeError as e:
            log.error(f"Exchange error: {e}")
            time.sleep(30)

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            time.sleep(30)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="XAUUSD.P BloFin Live Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate signals without placing real orders",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)
