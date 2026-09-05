from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from config import Config
from strategy import evaluate_strategy


def candles() -> pd.DataFrame:
    n = 120
    close = np.linspace(50.0, 103.0, n)
    close[-8:] = [104, 106, 108, 110, 106, 103, 101, 100]
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * 1.001
    low = np.minimum(open_, close) * 0.999
    volume = np.full(n, 100.0)
    volume[-8] = 150.0
    return pd.DataFrame({"timestamp": np.arange(n) * 900_000, "open": open_, "high": high,
                         "low": low, "close": close, "volume": volume})


def config(**kwargs) -> Config:
    return replace(Config(), **kwargs)


def test_positive_strategy_and_levels():
    ev = evaluate_strategy("TEST/USDT:USDT", candles(), 4_000_000, config(account_size=10_000, risk_pct=1))
    assert ev.passed
    assert all(x.passed for x in ev.criteria.values())
    assert ev.levels is not None
    assert ev.levels.entry == 100
    assert ev.levels.tp1 == pytest.approx(94.5)
    assert ev.levels.tp2 == pytest.approx(90)
    assert ev.levels.tp3 == pytest.approx(85)
    assert ev.levels.sl == pytest.approx(111.24)
    assert ev.levels.position_notional == pytest.approx(681.1797753)
    assert ev.levels.position_quantity == pytest.approx(7.0224719)
    assert ev.levels.margin_required == pytest.approx(227.0599251)


@pytest.mark.parametrize(("criterion", "quote_volume", "changes"), [
    ("pump", 4_000_000, {"pump_1h_pct": 500, "pump_4h_pct": 500, "pump_24h_pct": 500}),
    ("quote_volume", 2_999_999, {}),
    ("rsi_or_super_pump", 4_000_000, {"min_rsi_15m": 101, "super_pump_pct": 500}),
    ("peak_distance", 4_000_000, {"max_peak_distance_pct": 8, "min_retracement_pct": 5}),
    ("retracement", 4_000_000, {"min_retracement_pct": 9.5, "max_peak_distance_pct": 10}),
    ("volume_spike", 4_000_000, {"min_volume_ratio": 2}),
    ("recent_volume_cooling", 4_000_000, {"max_recent_volume_ratio": 0.9}),
])
def test_negative_each_numeric_criterion(criterion, quote_volume, changes):
    ev = evaluate_strategy("TEST/USDT:USDT", candles(), quote_volume, config(**changes))
    assert not ev.passed
    assert not ev.criteria[criterion].passed


def test_negative_price_not_rising():
    df = candles()
    df.loc[df.index[-2], "close"] = 99
    df.loc[df.index[-1], "close"] = 100
    ev = evaluate_strategy("TEST/USDT:USDT", df, 4_000_000, config())
    assert not ev.passed
    assert not ev.criteria["price_not_rising"].passed


def test_requires_120_bars():
    with pytest.raises(ValueError, match="120"):
        evaluate_strategy("X/USDT:USDT", candles().iloc[-119:], 4_000_000, config())
