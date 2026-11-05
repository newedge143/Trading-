"""Pure cost/accounting helpers (fees, spread, slippage, funding, liquidation)."""

from __future__ import annotations

import math

from tradinggym.config import ContractSpec, CostConfig


def fee(notional: float, costs: CostConfig, maker: bool | None = None) -> float:
    """Exchange fee for a fill of absolute `notional`."""
    maker = costs.use_maker if maker is None else maker
    rate = costs.maker_fee if maker else costs.taker_fee
    return abs(notional) * rate * costs.fee_multiplier


def fill_price(reference: float, side: int, spec: ContractSpec, costs: CostConfig):
    """Price paid for a market order of `side` (+1 buy / -1 sell).

    Returns (price, spread_cost_per_unit, slippage_cost_per_unit).  Buyers pay
    above the reference (mid), sellers receive below it.
    """
    if side not in (1, -1):
        raise ValueError("side must be +1 or -1")
    half_spread = reference * spec.half_spread_bps * 1e-4 * costs.spread_multiplier
    slip = reference * spec.slippage_bps * 1e-4 * costs.slippage_multiplier
    return reference + side * (half_spread + slip), half_spread, slip


def funding_payment(position_qty: float, mark_price: float, rate: float,
                    costs: CostConfig) -> float:
    """Cash flow to the account from one funding event.

    Positive funding rate: longs pay shorts.  Returns a signed amount
    (negative = paid).  Under `adverse_funding` stress the absolute value is
    always charged.
    """
    notional = position_qty * mark_price
    flow = -notional * rate
    if costs.adverse_funding:
        return -abs(flow) if flow != 0 else -abs(notional) * abs(rate)
    return flow


def round_qty(qty: float, spec: ContractSpec) -> float:
    """Round absolute quantity DOWN to the lot step, preserving sign."""
    if qty == 0:
        return 0.0
    steps = math.floor(abs(qty) / spec.lot_step + 1e-9)
    return math.copysign(round(steps * spec.lot_step, 12), qty)


def maintenance_margin(abs_notional: float, spec: ContractSpec) -> float:
    return abs_notional * spec.maintenance_margin_rate


def liquidation_price(entry: float, qty: float, wallet_alloc: float,
                      spec: ContractSpec) -> float:
    """Isolated-margin liquidation price for a single position.

    Solves wallet_alloc + qty*(P-entry) = |qty|*P*mmr.
    """
    mmr = spec.maintenance_margin_rate
    if qty > 0:
        return max(0.0, (qty * entry - wallet_alloc) / (qty * (1 - mmr)))
    q = abs(qty)
    return (wallet_alloc + q * entry) / (q * (1 + mmr))
