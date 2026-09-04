import pytest

from config import Config


def test_loads_env_and_unified_symbols():
    cfg = Config.from_env({"SCANNER_INTERVAL": "60", "OHLCV_LIMIT": "120", "LEVERAGE": "5",
                           "SYMBOLS": "BTC/USDT:USDT, ETH/USDT:USDT", "ACCOUNT_SIZE": "1000"})
    assert cfg.scanner_interval == 60
    assert cfg.leverage == 5
    assert cfg.symbols == ("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg.account_size == 1000
    assert cfg.timeframe == "15m"
    assert cfg.timeframe_minutes == 15


@pytest.mark.parametrize("value", ["", "ALL", "all", "*", "SYMBOLS="])
def test_full_market_aliases(value):
    cfg = Config.from_env({"SYMBOLS": value})
    assert cfg.symbols == ()


def test_symbols_are_normalized_to_uppercase():
    cfg = Config.from_env({"SYMBOLS": "btc/usdt:usdt"})
    assert cfg.symbols == ("BTC/USDT:USDT",)


@pytest.mark.parametrize("env", [
    {"SCANNER_INTERVAL": "9"}, {"OHLCV_LIMIT": "119"}, {"LEVERAGE": "0"},
    {"RISK_PCT": "101"}, {"ACCOUNT_SIZE": "-1"}, {"TP1_PCT": "12", "TP2_PCT": "10"},
    {"MIN_RETRACEMENT_PCT": "11", "MAX_PEAK_DISTANCE_PCT": "10"}, {"PORT": "70000"},
    {"PUMP_1H_PCT": "abc"}, {"TIMEFRAME": "7m"}, {"SYMBOLS": "BTCUSDT"},
])
def test_invalid_config(env):
    with pytest.raises(ValueError):
        Config.from_env(env)
