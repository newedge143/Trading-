"""
Technical indicators for the Acceleration Breakout strategy.
"""

import numpy as np
import pandas as pd


def roc(close: pd.Series, period: int) -> pd.Series:
    """Rate of Change: percentage change over `period` bars."""
    return close.pct_change(period) * 100


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def bollinger_bands(close: pd.Series, period: int = 20, std: float = 2.0):
    """Returns (upper, middle, lower) Bollinger Bands."""
    mid = close.rolling(period).mean()
    sigma = close.rolling(period).std(ddof=0)
    return mid + std * sigma, mid, mid - std * sigma


def keltner_channels(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
    mult: float = 1.5,
):
    """Returns (upper, middle, lower) Keltner Channels."""
    mid = close.rolling(period).mean()
    _atr = atr(high, low, close, period)
    return mid + mult * _atr, mid, mid - mult * _atr


def bb_squeeze(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    bb_period: int = 20,
    bb_std: float = 2.0,
    kc_mult: float = 1.5,
) -> pd.Series:
    """
    True when BB bands are inside Keltner Channels (low-volatility squeeze).
    This marks a period of tight consolidation.
    """
    bb_upper, _, bb_lower = bollinger_bands(close, bb_period, bb_std)
    kc_upper, _, kc_lower = keltner_channels(high, low, close, bb_period, kc_mult)
    return (bb_upper <= kc_upper) & (bb_lower >= kc_lower)


def momentum_histogram(close: pd.Series, fast: int, slow: int) -> pd.Series:
    """
    Acceleration histogram: fast ROC minus slow ROC.
    Positive = momentum is accelerating upward.
    Negative = momentum is accelerating downward.
    """
    return roc(close, fast) - roc(close, slow)


def consolidation_range(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
    atr_mult: float = 0.5,
):
    """
    Identify tight consolidation zones.
    Returns (in_consolidation, range_high, range_low).
    `in_consolidation` is True when the rolling range < atr_mult * ATR.
    """
    roll_high = high.rolling(period).max()
    roll_low = low.rolling(period).min()
    roll_range = roll_high - roll_low
    _atr = atr(high, low, close, period)
    in_consol = roll_range < (atr_mult * _atr)
    return in_consol, roll_high, roll_low


def volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    return volume.rolling(period).mean()
