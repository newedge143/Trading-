"""Basic sanity tests for indicators."""

import numpy as np
import pandas as pd
import pytest

from src.indicators import atr, bollinger_bands, momentum_histogram, roc, bb_squeeze


def _make_ohlcv(n=100):
    np.random.seed(42)
    close = pd.Series(100 + np.cumsum(np.random.randn(n) * 0.5))
    high = close + abs(np.random.randn(n) * 0.3)
    low = close - abs(np.random.randn(n) * 0.3)
    volume = pd.Series(np.random.randint(1000, 5000, n).astype(float))
    return high, low, close, volume


def test_roc_length():
    _, _, close, _ = _make_ohlcv()
    result = roc(close, 5)
    assert len(result) == len(close)
    assert result.iloc[:5].isna().all() or result.iloc[0] != result.iloc[0]


def test_atr_positive():
    high, low, close, _ = _make_ohlcv()
    result = atr(high, low, close, 14)
    assert (result.dropna() > 0).all()


def test_bollinger_bands_order():
    _, _, close, _ = _make_ohlcv()
    upper, mid, lower = bollinger_bands(close, 20, 2.0)
    valid = upper.dropna()
    assert (valid > mid.dropna()).all()
    assert (mid.dropna() > lower.dropna()).all()


def test_momentum_histogram():
    _, _, close, _ = _make_ohlcv()
    hist = momentum_histogram(close, 5, 14)
    assert len(hist) == len(close)
    assert not hist.dropna().empty


def test_bb_squeeze_returns_bool():
    high, low, close, _ = _make_ohlcv(200)
    result = bb_squeeze(high, low, close)
    assert result.dtype == bool
