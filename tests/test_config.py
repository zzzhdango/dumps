import pytest

from config import Config


def test_default_runtime_intervals_are_preserved():
    cfg = Config.from_env({})
    assert cfg.scanner_interval == 300
    assert cfg.active_monitor_interval == 60


def test_loads_env_and_unified_symbols():
    cfg = Config.from_env({"SCANNER_INTERVAL": "60", "OHLCV_LIMIT": "120", "LEVERAGE": "5",
                           "SYMBOLS": "BTC/USDT:USDT, ETH/USDT:USDT", "ACCOUNT_SIZE": "1000"})
    assert cfg.scanner_interval == 60
    assert cfg.leverage == 5
    assert cfg.symbols == ("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert cfg.account_size == 1000
    assert cfg.timeframe == "15m"
    assert cfg.timeframe_minutes == 15
    assert cfg.admin_ids == (401028479,)
    assert cfg.scan_concurrency == 5
    assert cfg.active_monitor_interval == 60
    assert cfg.active_monitor_concurrency == 3
    assert not hasattr(cfg, "bingx_api_key")
    assert not hasattr(cfg, "paused_recheck_interval")


def test_admin_ids_are_parsed_from_comma_separated_value():
    cfg = Config.from_env({"ADMIN_IDS": "401028479, 987654321"})
    assert cfg.admin_ids == (401028479, 987654321)


def test_scanner_scheduling_settings_are_loaded():
    cfg = Config.from_env({
        "SCAN_CONCURRENCY": "7",
        "ACTIVE_MONITOR_INTERVAL": "90",
        "ACTIVE_MONITOR_CONCURRENCY": "4",
    })
    assert cfg.scan_concurrency == 7
    assert cfg.active_monitor_interval == 90
    assert cfg.active_monitor_concurrency == 4


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
    {"ENTRY_ZONE_PCT": "0"}, {"SL_ABOVE_ZONE_PCT": "0"}, {"SIGNAL_VALID_HOURS": "0"},
    {"ADMIN_IDS": ""}, {"ADMIN_IDS": "401028479,nope"}, {"ADMIN_IDS": "-1"},
    {"SCAN_CONCURRENCY": "0"}, {"SCAN_CONCURRENCY": "11"},
    {"ACTIVE_MONITOR_INTERVAL": "29"},
    {"ACTIVE_MONITOR_CONCURRENCY": "0"}, {"ACTIVE_MONITOR_CONCURRENCY": "6"},
])
def test_invalid_config(env):
    with pytest.raises(ValueError):
        Config.from_env(env)
