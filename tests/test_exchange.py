import pytest

from config import Config
from exchange import BingXPublicClient, is_paused_market_error


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


class FakeExchange:
    def __init__(self):
        self.reload_values = []
        self.markets = {
            "BTC/USDT:USDT": {"swap": True, "active": True, "quote": "USDT"},
            "OLD/USDT:USDT": {"swap": True, "active": True, "quote": "USDT"},
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

    await client.initialize()
    assert set(client.symbols) == {"BTC/USDT:USDT", "OLD/USDT:USDT"}

    client.exchange.markets = {
        "BTC/USDT:USDT": {"swap": True, "active": True, "quote": "USDT"},
        "NEW/USDT:USDT": {"swap": True, "active": True, "quote": "USDT"},
    }
    added, removed = await client.refresh_markets()

    assert added == {"NEW/USDT:USDT"}
    assert removed == {"OLD/USDT:USDT"}
    assert client.exchange.reload_values == [False, True]


def test_paused_markets_are_reset_for_next_cycle():
    client = object.__new__(BingXPublicClient)
    client.unavailable_symbols = {"PAUSED/USDT:USDT"}
    previous = client.reset_paused_for_recheck()
    assert previous == {"PAUSED/USDT:USDT"}
    assert client.unavailable_symbols == set()


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
