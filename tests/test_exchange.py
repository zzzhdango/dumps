import pytest

import exchange
from config import Config
from exchange import (
    BingXPublicClient,
    MarketUnavailable,
    bingx_cooldown_until_ms,
    is_crypto_swap_market,
    is_paused_market_error,
)


@pytest.mark.parametrize("message", [
    'bingx {"code":109415,"msg":"ABC-USDT is pause currently"}',
    "BINGX: market is pause currently",
])
def test_detects_paused_bingx_market(message):
    assert is_paused_market_error(RuntimeError(message))


@pytest.mark.parametrize("message", [
    'bingx {"code":100001,"msg":"temporary problem"}',
    "another exchange is pause currently",
])
def test_does_not_hide_other_exchange_errors(message):
    assert not is_paused_market_error(RuntimeError(message))


def test_extracts_bingx_api_cooldown_timestamp():
    error = RuntimeError(
        'bingx {"code":109429,"msg":"retry after time: 1788608949332"}'
    )
    assert bingx_cooldown_until_ms(error) == 1788608949332
    assert bingx_cooldown_until_ms(RuntimeError("other error")) is None


def test_crypto_status_excludes_tradfi_status():
    base = {"swap": True, "active": True, "quote": "USDT"}
    assert is_crypto_swap_market({**base, "info": {"status": 1}})
    assert not is_crypto_swap_market({**base, "info": {"status": 25}})


class FakeExchange:
    def __init__(self):
        self.reload_values = []
        self.markets = {
            "BTC/USDT:USDT": {
                "swap": True,
                "active": True,
                "quote": "USDT",
                "info": {"status": 1},
            },
            "OLD/USDT:USDT": {
                "swap": True,
                "active": True,
                "quote": "USDT",
                "info": {"status": 1},
            },
            "NCFXEUR2USD/USDT:USDT": {
                "swap": True,
                "active": True,
                "quote": "USDT",
                "info": {"status": 25},
            },
        }

    async def load_markets(self, reload=False):
        self.reload_values.append(reload)
        return self.markets


@pytest.mark.asyncio
async def test_refresh_markets_adds_and_removes_symbols():
    client = object.__new__(BingXPublicClient)
    client.cfg = Config.from_env({"SYMBOLS": "ALL"})
    client.exchange = FakeExchange()
    client.symbols = []
    client.unavailable_symbols = set()
    client.paused_retry_at = {}
    client.paused_probe_attempts = exchange.deque()
    client.authorized_paused_probes = set()
    client.api_cooldown_until_ms = 0

    await client.initialize()
    assert set(client.symbols) == {"BTC/USDT:USDT", "OLD/USDT:USDT"}

    client.exchange.markets = {
        "BTC/USDT:USDT": {
            "swap": True,
            "active": True,
            "quote": "USDT",
            "info": {"status": 1},
        },
        "NEW/USDT:USDT": {
            "swap": True,
            "active": True,
            "quote": "USDT",
            "info": {"status": 1},
        },
    }
    added, removed = await client.refresh_markets()

    assert added == {"NEW/USDT:USDT"}
    assert removed == {"OLD/USDT:USDT"}
    assert client.exchange.reload_values == [False, True]


def test_paused_markets_are_rechecked_in_bounded_due_batch():
    client = object.__new__(BingXPublicClient)
    client.cfg = Config.from_env({
        "PAUSED_RECHECK_INTERVAL": "900",
        "PAUSED_RECHECK_BATCH": "2",
    })
    client.symbols = [
        "ACTIVE/USDT:USDT",
        "PAUSED1/USDT:USDT",
        "PAUSED2/USDT:USDT",
        "PAUSED3/USDT:USDT",
        "WAIT/USDT:USDT",
    ]
    client.unavailable_symbols = {
        "PAUSED1/USDT:USDT",
        "PAUSED2/USDT:USDT",
        "PAUSED3/USDT:USDT",
        "WAIT/USDT:USDT",
    }
    client.paused_retry_at = {
        "PAUSED1/USDT:USDT": 0,
        "PAUSED2/USDT:USDT": 0,
        "PAUSED3/USDT:USDT": 0,
        "WAIT/USDT:USDT": float("inf"),
    }
    client.paused_probe_attempts = exchange.deque()
    client.authorized_paused_probes = set()

    symbols, previous, retrying = client.prepare_scan_symbols()

    assert previous == {
        "PAUSED1/USDT:USDT",
        "PAUSED2/USDT:USDT",
        "PAUSED3/USDT:USDT",
        "WAIT/USDT:USDT",
    }
    assert retrying == {
        "PAUSED1/USDT:USDT",
        "PAUSED2/USDT:USDT",
    }
    assert symbols == [
        "ACTIVE/USDT:USDT",
        "PAUSED1/USDT:USDT",
        "PAUSED2/USDT:USDT",
    ]
    assert {
        "PAUSED1/USDT:USDT",
        "PAUSED2/USDT:USDT",
    }.issubset(client.unavailable_symbols)


def test_paused_probe_budget_is_global_across_cycles(monkeypatch):
    client = object.__new__(BingXPublicClient)
    client.cfg = Config.from_env({"PAUSED_RECHECK_BATCH": "5"})
    client.symbols = [
        f"PAUSED{i}/USDT:USDT" for i in range(10)
    ]
    client.unavailable_symbols = set(client.symbols)
    client.paused_retry_at = {symbol: 0 for symbol in client.symbols}
    client.paused_probe_attempts = exchange.deque()
    client.authorized_paused_probes = set()
    now = 1000.0
    monkeypatch.setattr(exchange.time, "monotonic", lambda: now)

    _, _, first = client.prepare_scan_symbols()
    _, _, second = client.prepare_scan_symbols()

    assert len(first) == 5
    assert second == set()
    assert len(client.paused_probe_attempts) == 5

    now += 901
    client.authorized_paused_probes.clear()
    _, _, third = client.prepare_scan_symbols()
    assert len(third) == 5


@pytest.mark.asyncio
async def test_known_paused_market_cannot_bypass_probe_budget():
    client = object.__new__(BingXPublicClient)
    client.unavailable_symbols = {"PAUSED/USDT:USDT"}
    client.authorized_paused_probes = set()
    with pytest.raises(MarketUnavailable, match="безопасной повторной проверки"):
        await client.fetch_market("PAUSED/USDT:USDT")


@pytest.mark.asyncio
async def test_paused_probe_stays_paused_after_network_failure():
    class FailingExchange:
        async def fetch_ohlcv(self, symbol, timeframe, limit):
            raise exchange.ccxt.NetworkError("temporary outage")

    client = object.__new__(BingXPublicClient)
    client.cfg = Config(max_retries=1)
    client.exchange = FailingExchange()
    client.unavailable_symbols = {"PAUSED/USDT:USDT"}
    client.paused_retry_at = {"PAUSED/USDT:USDT": 0}
    client.authorized_paused_probes = {"PAUSED/USDT:USDT"}
    client.api_cooldown_until_ms = 0

    with pytest.raises(exchange.ccxt.NetworkError):
        await client.fetch_market("PAUSED/USDT:USDT")

    assert "PAUSED/USDT:USDT" in client.unavailable_symbols
    assert client.paused_retry_at["PAUSED/USDT:USDT"] > exchange.time.monotonic()


@pytest.mark.asyncio
async def test_109429_retries_without_consuming_normal_retry_limit():
    client = object.__new__(BingXPublicClient)
    client.cfg = Config(max_retries=1)
    client.api_cooldown_until_ms = 0
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise exchange.ccxt.ExchangeError(
                'bingx {"code":109429,'
                '"msg":"retry after time: 1000000000000"}'
            )
        return "ok"

    assert await client._retry(operation, "test") == "ok"
    assert calls == 2


@pytest.mark.parametrize("query", [
    "btc",
    "BTCUSDT",
    "btc/usdt:usdt",
    " BTC ",
])
def test_resolves_manual_symbol_formats(query):
    client = object.__new__(BingXPublicClient)
    client.available_symbols = {"BTC/USDT:USDT", "ETH/USDT:USDT"}
    assert client.resolve_symbol(query) == "BTC/USDT:USDT"


def test_rejects_unknown_manual_symbol():
    client = object.__new__(BingXPublicClient)
    client.available_symbols = {"BTC/USDT:USDT"}
    assert client.resolve_symbol("UNKNOWN") is None
