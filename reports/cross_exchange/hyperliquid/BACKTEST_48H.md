# 48-Hour Backtest — NOT EXECUTED

Generated: 2026-11-05T02:28:36.822302+00:00

**Status: the backtest did not run, because the required historical market data could not be retrieved.** No prices, trades or results were simulated or substituted.

- Requested test window: `2026-11-02T00:00:00Z` → `2026-11-04T00:00:00Z` (exclusive)
- Primary exchange: `hyperliquid` (https://api.hyperliquid.xyz/info)
- Universe: BTCUSDT, ETHUSDT, SOLUSDT

## Missing data

| Symbol | Missing intervals in test window |
|---|---|
| BTCUSDT | 2026-11-02T00:00:00Z → 2026-11-04T00:00:00Z |
| ETHUSDT | 2026-11-02T00:00:00Z → 2026-11-04T00:00:00Z |
| SOLUSDT | 2026-11-02T00:00:00Z → 2026-11-04T00:00:00Z |

## Retrieval errors

- `BTCUSDT` klines: https://api.hyperliquid.xyz/info: ProxyError: HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Max retries exceeded with url: /info (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `BTCUSDT` funding: https://api.hyperliquid.xyz/info: ProxyError: HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Max retries exceeded with url: /info (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `ETHUSDT` klines: https://api.hyperliquid.xyz/info: ProxyError: HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Max retries exceeded with url: /info (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `ETHUSDT` funding: https://api.hyperliquid.xyz/info: ProxyError: HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Max retries exceeded with url: /info (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `SOLUSDT` klines: https://api.hyperliquid.xyz/info: ProxyError: HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Max retries exceeded with url: /info (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))
- `SOLUSDT` funding: https://api.hyperliquid.xyz/info: ProxyError: HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Max retries exceeded with url: /info (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))

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
