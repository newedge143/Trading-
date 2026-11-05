"""
Historical market-data retrieval, storage, manifest and coverage checking.

Supported public (no API key) sources:
  * binance     - fapi.binance.com  (USDT-M perpetual klines, funding, exchangeInfo)
  * bybit       - api.bybit.com     (v5 linear klines, funding history, instruments)
  * hyperliquid - api.hyperliquid.xyz (candleSnapshot, fundingHistory)

Nothing in this module ever creates prices.  If a request fails the failure is
recorded in the manifest and the caller decides how to proceed.

Stored format (data/historical/<exchange>/):
  <SYMBOL>_<interval>_klines.csv : timestamp(ms, bar open), open, high, low,
                                   close, volume, quote_volume
  <SYMBOL>_funding.csv           : timestamp(ms), funding_rate, mark_price
  contract_specs.json            : raw + parsed contract specifications
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
               "4h": 14_400_000, "1d": 86_400_000}

KLINE_COLS = ["timestamp", "open", "high", "low", "close", "volume", "quote_volume"]
FUNDING_COLS = ["timestamp", "funding_rate", "mark_price"]


def to_ms(ts: str | pd.Timestamp) -> int:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return int(t.value // 1_000_000)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────

class DataUnavailable(RuntimeError):
    pass


def _http(method: str, url: str, *, params=None, json_body=None, retries=3, timeout=20):
    if requests is None:
        raise DataUnavailable("python 'requests' package not installed")
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, params=params, json=json_body, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise DataUnavailable(f"HTTP {r.status_code} from {url}: {r.text[:200]}")
            return r.json()
        except DataUnavailable:
            raise
        except Exception as exc:  # network/proxy errors
            last = exc
            time.sleep(1 + attempt)
    raise DataUnavailable(f"{url}: {type(last).__name__}: {last}")


# ─────────────────────────────────────────────────────────────────────────────
# Provider implementations
# ─────────────────────────────────────────────────────────────────────────────

class Provider:
    name = "base"
    funding_interval_hint = "8h"

    def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        raise NotImplementedError

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        raise NotImplementedError

    def contract_specs(self, symbols: List[str]) -> Dict[str, dict]:
        return {}

    def endpoint(self) -> str:
        return ""


class BinanceProvider(Provider):
    name = "binance"
    base = "https://fapi.binance.com"

    def endpoint(self):
        return self.base

    def klines(self, symbol, interval, start_ms, end_ms):
        rows, cur = [], start_ms
        while cur < end_ms:
            data = _http("GET", f"{self.base}/fapi/v1/klines",
                         params={"symbol": symbol, "interval": interval,
                                 "startTime": cur, "endTime": end_ms - 1, "limit": 1500})
            if not data:
                break
            rows.extend(data)
            nxt = int(data[-1][0]) + INTERVAL_MS[interval]
            if nxt <= cur:
                break
            cur = nxt
        df = pd.DataFrame([[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                            float(r[5]), float(r[7])] for r in rows], columns=KLINE_COLS)
        return _clean(df, start_ms, end_ms)

    def funding(self, symbol, start_ms, end_ms):
        rows, cur = [], start_ms
        while cur < end_ms:
            data = _http("GET", f"{self.base}/fapi/v1/fundingRate",
                         params={"symbol": symbol, "startTime": cur, "endTime": end_ms - 1,
                                 "limit": 1000})
            if not data:
                break
            rows.extend(data)
            nxt = int(data[-1]["fundingTime"]) + 1
            if nxt <= cur or len(data) < 1000:
                break
            cur = nxt
        df = pd.DataFrame([[int(r["fundingTime"]), float(r["fundingRate"]),
                            float(r.get("markPrice") or "nan")] for r in rows],
                          columns=FUNDING_COLS)
        return _clean(df, start_ms, end_ms)

    def contract_specs(self, symbols):
        info = _http("GET", f"{self.base}/fapi/v1/exchangeInfo")
        out = {}
        for s in info.get("symbols", []):
            if s["symbol"] not in symbols:
                continue
            f = {x["filterType"]: x for x in s.get("filters", [])}
            out[s["symbol"]] = {
                "tick_size": float(f["PRICE_FILTER"]["tickSize"]),
                "lot_step": float(f["MARKET_LOT_SIZE"]["stepSize"]),
                "min_qty": float(f["MARKET_LOT_SIZE"]["minQty"]),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
                "maintenance_margin_rate": float(s.get("maintMarginPercent", 2.5)) / 100.0,
                "raw_contract_type": s.get("contractType"),
            }
        return out


class BybitProvider(Provider):
    name = "bybit"
    base = "https://api.bybit.com"
    _iv = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}

    def endpoint(self):
        return self.base

    def klines(self, symbol, interval, start_ms, end_ms):
        rows, cur_end = [], end_ms - 1
        while cur_end >= start_ms:
            data = _http("GET", f"{self.base}/v5/market/kline",
                         params={"category": "linear", "symbol": symbol,
                                 "interval": self._iv[interval], "start": start_ms,
                                 "end": cur_end, "limit": 1000})
            lst = data.get("result", {}).get("list", [])
            if not lst:
                break
            rows.extend(lst)
            oldest = min(int(r[0]) for r in lst)
            if oldest <= start_ms or len(lst) < 1000:
                break
            cur_end = oldest - 1
        df = pd.DataFrame([[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                            float(r[5]), float(r[6])] for r in rows], columns=KLINE_COLS)
        return _clean(df, start_ms, end_ms)

    def funding(self, symbol, start_ms, end_ms):
        rows, cur_end = [], end_ms - 1
        while cur_end >= start_ms:
            data = _http("GET", f"{self.base}/v5/market/funding/history",
                         params={"category": "linear", "symbol": symbol,
                                 "startTime": start_ms, "endTime": cur_end, "limit": 200})
            lst = data.get("result", {}).get("list", [])
            if not lst:
                break
            rows.extend(lst)
            oldest = min(int(r["fundingRateTimestamp"]) for r in lst)
            if oldest <= start_ms or len(lst) < 200:
                break
            cur_end = oldest - 1
        df = pd.DataFrame([[int(r["fundingRateTimestamp"]), float(r["fundingRate"]),
                            float("nan")] for r in rows], columns=FUNDING_COLS)
        return _clean(df, start_ms, end_ms)


class HyperliquidProvider(Provider):
    name = "hyperliquid"
    base = "https://api.hyperliquid.xyz/info"
    funding_interval_hint = "1h"

    def endpoint(self):
        return self.base

    @staticmethod
    def _coin(symbol):
        return symbol.replace("USDT", "").replace("USD", "")

    def klines(self, symbol, interval, start_ms, end_ms):
        rows, cur = [], start_ms
        while cur < end_ms:
            data = _http("POST", self.base, json_body={
                "type": "candleSnapshot",
                "req": {"coin": self._coin(symbol), "interval": interval,
                        "startTime": cur, "endTime": end_ms - 1}})
            if not data:
                break
            rows.extend(data)
            nxt = int(data[-1]["t"]) + INTERVAL_MS[interval]
            if nxt <= cur:
                break
            cur = nxt
        df = pd.DataFrame([[int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]),
                            float(r["c"]), float(r["v"]), float(r["v"]) * float(r["c"])]
                           for r in rows], columns=KLINE_COLS)
        return _clean(df, start_ms, end_ms)

    def funding(self, symbol, start_ms, end_ms):
        rows, cur = [], start_ms
        while cur < end_ms:
            data = _http("POST", self.base, json_body={
                "type": "fundingHistory", "coin": self._coin(symbol),
                "startTime": cur, "endTime": end_ms - 1})
            if not data:
                break
            rows.extend(data)
            nxt = int(data[-1]["time"]) + 1
            if nxt <= cur or len(data) < 500:
                break
            cur = nxt
        df = pd.DataFrame([[int(r["time"]), float(r["fundingRate"]), float("nan")]
                           for r in rows], columns=FUNDING_COLS)
        return _clean(df, start_ms, end_ms)


PROVIDERS = {"binance": BinanceProvider, "bybit": BybitProvider,
             "hyperliquid": HyperliquidProvider}


def _clean(df: pd.DataFrame, start_ms: int, end_ms: int) -> pd.DataFrame:
    if df.empty:
        return df
    df = df[(df["timestamp"] >= start_ms) & (df["timestamp"] < end_ms)]
    return df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Coverage
# ─────────────────────────────────────────────────────────────────────────────

def missing_intervals(timestamps_ms, start_ms: int, end_ms: int,
                      step_ms: int) -> List[Tuple[str, str]]:
    """Return [(gap_start_iso, gap_end_iso_exclusive), ...] for absent bars."""
    have = set(int(t) for t in timestamps_ms)
    gaps, gap_start = [], None
    t = start_ms
    while t < end_ms:
        if t not in have:
            gap_start = t if gap_start is None else gap_start
        elif gap_start is not None:
            gaps.append((gap_start, t))
            gap_start = None
        t += step_ms
    if gap_start is not None:
        gaps.append((gap_start, end_ms))
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [(iso(a), iso(b)) for a, b in gaps]


# ─────────────────────────────────────────────────────────────────────────────
# Download orchestration + manifest
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DownloadResult:
    ok: bool
    manifest: dict
    klines: Dict[str, pd.DataFrame] = field(default_factory=dict)
    funding: Dict[str, pd.DataFrame] = field(default_factory=dict)
    specs: Dict[str, dict] = field(default_factory=dict)


def download(exchange: str, symbols: List[str], interval: str, fetch_start: str,
             fetch_end: str, test_start: str, test_end: str, out_dir: Path,
             provider: Optional[Provider] = None) -> DownloadResult:
    provider = provider or PROVIDERS[exchange]()
    out = Path(out_dir) / exchange
    out.mkdir(parents=True, exist_ok=True)
    s_ms, e_ms = to_ms(fetch_start), to_ms(fetch_end)
    ts_ms, te_ms = to_ms(test_start), to_ms(test_end)
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "exchange": exchange, "endpoint": provider.endpoint(), "interval": interval,
        "requested_fetch_window": [fetch_start, fetch_end],
        "required_test_window": [test_start, test_end],
        "retrieval_started_utc": now, "files": [], "errors": [], "coverage": {},
        "contract_specs_source": None,
    }
    res = DownloadResult(ok=True, manifest=manifest)
    for sym in symbols:
        for kind in ("klines", "funding"):
            try:
                if kind == "klines":
                    df = provider.klines(sym, interval, s_ms, e_ms)
                    path = out / f"{sym}_{interval}_klines.csv"
                else:
                    df = provider.funding(sym, s_ms, e_ms)
                    path = out / f"{sym}_funding.csv"
                df.to_csv(path, index=False)
                getattr(res, kind)[sym] = df
                manifest["files"].append({
                    "symbol": sym, "kind": kind, "path": str(path), "rows": int(len(df)),
                    "first_ts": _iso(df["timestamp"].min()) if len(df) else None,
                    "last_ts": _iso(df["timestamp"].max()) if len(df) else None,
                    "sha256": sha256_file(path),
                    "retrieved_utc": datetime.now(timezone.utc).isoformat()})
            except DataUnavailable as exc:
                res.ok = False
                manifest["errors"].append({"symbol": sym, "kind": kind, "error": str(exc)})
        kl = res.klines.get(sym)
        gaps = missing_intervals(kl["timestamp"] if kl is not None else [], ts_ms, te_ms,
                                 INTERVAL_MS[interval])
        manifest["coverage"][sym] = {"test_window_missing_intervals": gaps,
                                     "complete": not gaps}
        if gaps:
            res.ok = False
    try:
        res.specs = provider.contract_specs(symbols)
        if res.specs:
            p = out / "contract_specs.json"
            p.write_text(json.dumps(res.specs, indent=2))
            manifest["contract_specs_source"] = f"{exchange} exchangeInfo (current snapshot, not point-in-time)"
    except DataUnavailable as exc:
        manifest["errors"].append({"symbol": "*", "kind": "contract_specs", "error": str(exc)})
    if not res.specs:
        manifest["contract_specs_source"] = "tradinggym.config.DEFAULT_SPECS (documented assumptions)"
    manifest["retrieval_finished_utc"] = datetime.now(timezone.utc).isoformat()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return res


def load_local(exchange: str, symbols: List[str], interval: str, data_dir: Path):
    """Load previously downloaded CSVs (e.g. on a machine with network access)."""
    d = Path(data_dir) / exchange
    kl, fu, files = {}, {}, []
    for sym in symbols:
        kp, fp = d / f"{sym}_{interval}_klines.csv", d / f"{sym}_funding.csv"
        if kp.exists():
            kl[sym] = pd.read_csv(kp)
            files.append({"symbol": sym, "kind": "klines", "path": str(kp),
                          "rows": len(kl[sym]), "sha256": sha256_file(kp)})
        if fp.exists():
            fu[sym] = pd.read_csv(fp)
            files.append({"symbol": sym, "kind": "funding", "path": str(fp),
                          "rows": len(fu[sym]), "sha256": sha256_file(fp)})
    specs = {}
    sp = d / "contract_specs.json"
    if sp.exists():
        specs = json.loads(sp.read_text())
    return kl, fu, specs, files


def _iso(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def align_panel(klines: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """Index each symbol by UTC bar-open timestamp on the common timeline.

    Missing bars are left as NaN rows (never forward-filled into tradable
    prices) so the engine can refuse to trade a symbol with no real bar.
    """
    idx = sorted(set().union(*[set(df["timestamp"].astype("int64")) for df in klines.values()]))
    out = {}
    for sym, df in klines.items():
        d = df.copy()
        d["timestamp"] = d["timestamp"].astype("int64")
        d = d.set_index("timestamp").reindex(idx)
        d.index = pd.to_datetime(d.index, unit="ms", utc=True)
        out[sym] = d
    return out
