# Project Audit — TradingGym AI

Audit date: 2026-11-05 (UTC). Repository: `newedge143/trading-`, branch `claude/build-trading-bot-wZAaA`, audited at commit `f32532b`.

> **Location note.** The brief names `F:\brAInfection`. That path isn't reachable from this session. The work was done in the cloned repository at `/home/user/Trading-`. Everything is plain Python with relative paths, so it runs unchanged from `F:\brAInfection` once the branch is pulled there.

## 1. What existed before this work

| Area | Finding |
|---|---|
| Strategies | `src/strategy.py` (Acceleration Breakout, XAUUSD-oriented), `src/scalp_strategy.py`, `src/bb_reversion.py`, `src/trend_pullback.py`, `src/hybrid_strategy.py` plus `src/regime.py` (regime router). All are single-symbol intraday TA strategies. **No momentum + funding or cross-sectional strategy.** |
| Backtesting | `src/backtest.py`: a single-symbol bar loop with SL/TP and a flat commission. Also 12 standalone `backtest_*.py` / `compound_scalper*.py` scripts that each re-implement a loop. The gaps were funding, spread, slippage, liquidation, margin, multi-asset portfolio and look-ahead guards. `src/backtest.py` also resolves same-bar TP+SL in the strategy's favour. |
| Exchange connectors | ccxt-based (`src/bot.py`, `src/data_fetcher.py`, `live_bot*.py`), mainly BloFin/Binance. `live_bot.py` / `live_bot_multi.py` can place **real orders** when API keys are present. |
| MT5 connector | **None** (no MetaTrader5 imports anywhere). |
| Database | **None** (CSV/log files only). |
| AI agents | **None.** |
| Dashboard | **None.** |
| Historical data | **None in repo.** `data/*.csv` is git-ignored and the directory doesn't exist. |
| Config | `config/config.yaml` (XAUUSD breakout parameters), `.env.example`. |
| Dependencies | `requirements.txt` pins old versions (pandas 2.2.2, numpy 1.26.4, ccxt 4.3.89). The container has pandas 3.0.5 and numpy 2.5.3. ccxt, ta and loguru are not installed here. |
| Tests | `tests/test_indicators.py`: 5 indicator sanity tests (all pass). Engines, strategies and risk had no tests. |

## 2. What was added (no existing file was modified)

All new code lives in a separate package, so existing modules stay untouched. Git history is the backup: `f32532b` is the pre-change state. Because no existing file was edited, there was nothing to copy aside.

```
tradinggym/
  config.py       Run/strategy/risk/cost configs; LIVE_TRADING_ENABLED = False (hard constant); 10x cap enforced
  data.py         Binance / Bybit / Hyperliquid public-API downloaders, CSV storage, SHA-256 manifest, gap detection
  costs.py        fee, spread, slippage, funding, lot rounding, maintenance margin, liquidation price
  strategy.py     14d momentum (70%) + funding carry (30%) cross-sectional scores -> long/short weights, drift metric
  agents.py       MarketScanner, MomentumAnalyst, FundingAnalyst, RiskManagerAgent, StrategyEvaluator + JSONL DecisionLog
  engine.py       event-driven multi-asset perp backtester (see BACKTEST_48H.md / module docstring)
  metrics.py      all 21 requested metrics + block-bootstrap liquidation probability
  experiments.py  leverage grid A-D x {equity, fixed_notional}, stress test, walk-forward experimental mode
  reporting.py    CSV/Markdown writers, 5 charts
run_48h_backtest.py        CLI entry point (exit 2 = real data unavailable, nothing simulated)
tests/test_tradinggym.py   37 tests
requirements-tradinggym.txt
```

### Engine design (anti-bias measures)
- Bars are processed chronologically. Agents receive `iloc[:i+1]` slices only, and `assert_no_future` raises if a later row leaks in.
- Signals form at the close of bar *i* and fill at the **open of bar *i+1*** plus half-spread and slippage. Nothing fills at the close that produced the signal.
- Funding is settled at the real funding timestamps on positions held at that instant, before fills at the same timestamp. Rates are only visible to the signal after settlement.
- Partial fills are capped at 1% of the **previous** bar's volume, since the current bar's volume isn't known at the open.
- Stops are gap-aware (fill at the open when price gaps through the stop). Cross-margin liquidation is checked at every position's intrabar adverse extreme, with a 0.5% liquidation fee and equity floored at 0.
- Missing bars stay NaN. They are never forward-filled into tradable prices.
- Orders are one-way net per symbol with at most one pending order per symbol, so there are no duplicate orders.
- Exchange minimum notional and lot step are enforced (orders below them are rejected and logged).

## 3. Still missing / out of scope

| Item | Status |
|---|---|
| Historical data for 2026-11-02 → 2026-11-04 | **Couldn't be retrieved.** The session's network policy returns HTTP 403 for `fapi.binance.com`, `data.binance.vision`, `api.bybit.com`, `api.hyperliquid.xyz` (and OKX, Kraken, Coinbase, BloFin, CoinGecko). See `DATA_MANIFEST.json`. |
| Historical bid/ask spread | No free point-in-time source is wired in. Spread is an assumed per-symbol parameter. |
| Point-in-time contract specs | Uses the current `exchangeInfo` snapshot when reachable, otherwise documented defaults. |
| Top-30 universe | `MarketScanner.rank_universe` ranks by trailing quote volume without look-ahead. Pass `--symbols` with more perps once data can be downloaded. |
| Original publication | Not retrieved or verified. Reproduction mode uses the brief's parameters and is labelled UNVERIFIED. |
| MT5, database, dashboard | Don't exist and weren't needed for this backtest. Not built. |
| Live trading | Deliberately absent from `tradinggym`. **Warning:** the pre-existing `live_bot.py` / `live_bot_multi.py` can still trade real money if API keys are configured. |
