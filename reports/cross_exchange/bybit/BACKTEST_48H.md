# 48-Hour Backtest — NOT EXECUTED

Generated: 2026-11-05T02:27:57.104354+00:00

**Status: the backtest did not run, because the required historical market data could not be retrieved.** No prices, trades or results were simulated or substituted.

- Requested test window: `2026-11-02T00:00:00Z` → `2026-11-04T00:00:00Z` (exclusive)
- Primary exchange: `bybit` (https://api.bybit.com)
- Universe: BTCUSDT, ETHUSDT, SOLUSDT

## Missing data

| Symbol | Missing intervals in test window |
|---|---|
| BTCUSDT | 2026-11-02T00:00:00Z → 2026-11-04T00:00:00Z |
| ETHUSDT | 2026-11-02T00:00:00Z → 2026-11-04T00:00:00Z |
| SOLUSDT | 2026-11-02T00:00:00Z → 2026-11-04T00:00:00Z |

## Retrieval errors

- `BTCUSDT` klines: https://api.bybit.com/v5/market/kline: ProxyError: HTTPSConnectionPool(host='api.bybit.com', port=443): Max retries exceeded with url: /v5/market/kline?category=linear&symbol=BTCUSDT&interval=60&start=1787616000000&end=1793750399999&limit=1000 (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `BTCUSDT` funding: https://api.bybit.com/v5/market/funding/history: ProxyError: HTTPSConnectionPool(host='api.bybit.com', port=443): Max retries exceeded with url: /v5/market/funding/history?category=linear&symbol=BTCUSDT&startTime=1787616000000&endTime=1793750399999&limit=200 (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `ETHUSDT` klines: https://api.bybit.com/v5/market/kline: ProxyError: HTTPSConnectionPool(host='api.bybit.com', port=443): Max retries exceeded with url: /v5/market/kline?category=linear&symbol=ETHUSDT&interval=60&start=1787616000000&end=1793750399999&limit=1000 (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `ETHUSDT` funding: https://api.bybit.com/v5/market/funding/history: ProxyError: HTTPSConnectionPool(host='api.bybit.com', port=443): Max retries exceeded with url: /v5/market/funding/history?category=linear&symbol=ETHUSDT&startTime=1787616000000&endTime=1793750399999&limit=200 (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `SOLUSDT` klines: https://api.bybit.com/v5/market/kline: ProxyError: HTTPSConnectionPool(host='api.bybit.com', port=443): Max retries exceeded with url: /v5/market/kline?category=linear&symbol=SOLUSDT&interval=60&start=1787616000000&end=1793750399999&limit=1000 (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `SOLUSDT` funding: https://api.bybit.com/v5/market/funding/history: ProxyError: HTTPSConnectionPool(host='api.bybit.com', port=443): Max retries exceeded with url: /v5/market/funding/history?category=linear&symbol=SOLUSDT&startTime=1787616000000&endTime=1793750399999&limit=200 (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))

## Metrics

All metrics (starting/ending equity, P&L, win rate, trades, profit factor, drawdown, Sharpe, Sortino, fees, funding, slippage, liquidations, long/short and per-symbol performance) are **not available** because no simulation ran.
LEVERAGE_COMPARISON.csv, TRADE_LEDGER.csv and EQUITY_CURVE.csv contain headers only.

## How to complete the run

Run on a machine whose network can reach the exchange API:

```
python run_48h_backtest.py            # Binance (primary)
python run_48h_backtest.py --exchange bybit --reports reports/bybit
```

Or download the CSVs elsewhere into `data/historical/<exchange>/` and run `python run_48h_backtest.py --offline`.
