"""
Configuration objects for the TradingGym research backtester.

All parameters are explicit and serialisable so every run can be recorded in
reports/STRATEGY_CONFIGURATION.json.  LIVE TRADING IS NOT IMPLEMENTED in this
package: there is no order-routing code, and `LIVE_TRADING_ENABLED` is a hard
constant checked by the runner.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Dict, List

LIVE_TRADING_ENABLED = False  # hard-coded; this package only simulates.

BACKTEST_START = "2026-11-02T00:00:00Z"
BACKTEST_END = "2026-11-04T00:00:00Z"  # exclusive


@dataclass(frozen=True)
class ContractSpec:
    """Per-symbol contract specification (USDT-margined linear perpetual)."""

    symbol: str
    tick_size: float
    lot_step: float
    min_qty: float
    min_notional: float
    maintenance_margin_rate: float  # first tier
    max_leverage: float = 20.0
    # Assumed half-spread / slippage when no historical quote data exists.
    half_spread_bps: float = 1.0
    slippage_bps: float = 2.0


# Defaults for Binance USDT-M perpetuals (first tier).  These are documented
# assumptions; the data downloader overwrites them with exchangeInfo values
# whenever that endpoint is reachable, and the manifest records which was used.
DEFAULT_SPECS: Dict[str, ContractSpec] = {
    "BTCUSDT": ContractSpec("BTCUSDT", 0.1, 0.001, 0.001, 100.0, 0.004,
                            half_spread_bps=0.5, slippage_bps=1.0),
    "ETHUSDT": ContractSpec("ETHUSDT", 0.01, 0.001, 0.001, 20.0, 0.004,
                            half_spread_bps=0.5, slippage_bps=1.5),
    "SOLUSDT": ContractSpec("SOLUSDT", 0.01, 1.0, 1.0, 5.0, 0.005,
                            half_spread_bps=1.0, slippage_bps=3.0),
}


def default_spec(symbol: str) -> ContractSpec:
    if symbol in DEFAULT_SPECS:
        return DEFAULT_SPECS[symbol]
    # Conservative generic alt-perp assumption.
    return ContractSpec(symbol, 0.0001, 0.001, 0.001, 5.0, 0.01,
                        half_spread_bps=2.5, slippage_bps=5.0)


@dataclass
class CostConfig:
    taker_fee: float = 0.0005   # Binance USDT-M VIP0 taker 0.05 %
    maker_fee: float = 0.0002   # Binance USDT-M VIP0 maker 0.02 %
    use_maker: bool = False     # market orders -> taker
    fee_multiplier: float = 1.0
    slippage_multiplier: float = 1.0
    spread_multiplier: float = 1.0
    adverse_funding: bool = False  # stress: every funding event is a cost
    liquidation_fee: float = 0.005  # clearance/insurance fee on liquidation notional
    max_bar_participation: float = 0.01  # partial fills: <=1 % of bar volume


@dataclass
class StrategyConfig:
    name: str = "momentum_funding_v1"
    mode: str = "reproduction"  # reproduction | experimental
    universe: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    bar_interval: str = "1h"
    momentum_lookback_hours: int = 14 * 24
    momentum_weight: float = 0.70
    funding_weight: float = 0.30
    funding_lookback_events: int = 9  # ~3 days of 8h funding
    rebalance_drift: float = 0.20     # rebalance when weights drift 20 %
    allow_short: bool = True


@dataclass
class RiskConfig:
    max_leverage: float = 10.0            # experiment cap (hard ceiling 10x)
    sizing: str = "equity"                # equity | fixed_notional
    exposure_utilisation: float = 1.0     # gross notional = util * lev * base
    max_positions: int = 3
    max_position_weight: float = 0.60    # of gross exposure
    stop_loss_pct: float = 0.05           # per-position adverse move
    stop_cooldown_bars: int = 8
    daily_loss_limit: float = 0.25        # halt for rest of UTC day
    max_drawdown_limit: float = 0.50      # emergency shutdown
    funding_filter_8h: float = 0.0010     # skip if funding cost > 0.10 %/8h against us
    min_quote_volume_24h: float = 50_000_000.0

    def __post_init__(self):
        if self.max_leverage > 10.0:
            raise ValueError("max_leverage above 10x is not permitted in this experiment")
        if self.sizing not in ("equity", "fixed_notional"):
            raise ValueError("sizing must be 'equity' or 'fixed_notional'")


@dataclass
class RunConfig:
    initial_capital: float = 100.0
    target_equity: float = 1100.0
    start: str = BACKTEST_START
    end: str = BACKTEST_END
    exchange: str = "binance"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)

    def with_(self, **kw) -> "RunConfig":
        return replace(self, **kw)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["live_trading_enabled"] = LIVE_TRADING_ENABLED
        return d


LEVERAGE_EXPERIMENTS = {"A": 1.0, "B": 2.0, "C": 5.0, "D": 10.0}
