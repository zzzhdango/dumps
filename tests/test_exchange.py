import asyncio
import time

import pytest

import exchange
from config import Config
from exchange import (
    BinanceFuturesPublicClient,
    BinanceRestrictedLocation,
    MarketUnavailable,
    RequestLocalBinanceUSDM,
    is_binance_futures_market,
    retry_after_seconds,
)


def market(**overrides):
    value = {
        "swap": True,
        "linear": True,
        "quote": "USDT",
        "settle": "USDT",
        "active": True,
        "info": {"status": "TRADING", "contractType": "PERPETUAL"},
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "bad",
    [
        {"swap": False},
        {"linear": False},
        {"quote": "USDC"},
        {"settle": "USDC"},
        {"settle": "BTC"},
        {"active": False},
        {"active": None},
        {"info": {"status": "BREAK", "contractType": "PERPETUAL"}},
        {"info": {"status": "TRADING", "contractType": "CURRENT_QUARTER"}},
    ],
)
def test_market_filter_rejects_non_usdt_linear_perpetuals(bad):
    assert not is_binance_futures_market(market(**bad))


def test_market_filter_accepts_perpetual_and_optional_info_fields():
    assert is_binance_futures_market(market())
    assert is_binance_futures_market(market(info={}))


def test_constructor_uses_public_binanceusdm_with_timeout(monkeypatch):
    assert issubclass(RequestLocalBinanceUSDM, exchange.ccxt.binanceusdm)
    captured = {}
    sentinel = object()

    def factory(options):
        captured.update(options)
        return sentinel

    monkeypatch.setattr(exchange, "RequestLocalBinanceUSDM", factory)
    client = BinanceFuturesPublicClient(Config(request_timeout_ms=12_345))

    assert client.exchange is sentinel
    assert captured == {"enableRateLimit": True, "timeout": 12_345}
    assert "apiKey" not in captured
    assert "secret" not in captured


class FakeExchange:
    def __init__(self):
        self.reload_values = []
        self.markets = {
            "BTC/USDT:USDT": market(),
            "OLD/USDT:USDT": market(),
            "USDC/USDT:USDT": market(settle="USDC"),
            "QUARTER/USDT:USDT": market(
                info={
                    "status": "TRADING",
                    "contractType": "CURRENT_QUARTER",
                }
            ),
        }

    async def load_markets(self, reload=False):
        self.reload_values.append(reload)
        return self.markets


def client_with(fake, **config_overrides):
    client = object.__new__(BinanceFuturesPublicClient)
    client.cfg = Config(**config_overrides)
    client.exchange = fake
    client.symbols = []
    client.available_symbols = set()
    return client


@pytest.mark.asyncio
async def test_refresh_markets_adds_removes_and_forces_reload():
    fake = FakeExchange()
    client = client_with(fake)
    await client.initialize()
    assert client.symbols == ["BTC/USDT:USDT", "OLD/USDT:USDT"]

    fake.markets = {
        "BTC/USDT:USDT": market(),
        "NEW/USDT:USDT": market(),
    }
    added, removed = await client.refresh_markets()

    assert added == {"NEW/USDT:USDT"}
    assert removed == {"OLD/USDT:USDT"}
    assert fake.reload_values == [False, True]


@pytest.mark.asyncio
async def test_last_whitelist_delisting_commits_empty_scan_snapshot():
    fake = FakeExchange()
    client = client_with(fake)
    client.cfg = Config(symbols=("BTC/USDT:USDT",))
    await client.initialize()
    fake.markets = {"ETH/USDT:USDT": market()}

    added, removed = await client.refresh_markets()

    assert added == set()
    assert removed == {"BTC/USDT:USDT"}
    assert client.symbols == []
    assert client.available_symbols == {"ETH/USDT:USDT"}


@pytest.mark.asyncio
async def test_market_refresh_is_serialized():
    class ConcurrentExchange:
        def __init__(self):
            self.in_flight = 0
            self.max_in_flight = 0

        async def load_markets(self, reload=False):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0)
            self.in_flight -= 1
            return {"BTC/USDT:USDT": market()}

    fake = ConcurrentExchange()
    client = client_with(fake)
    await asyncio.gather(
        client.refresh_markets(),
        client.refresh_markets(),
        client.refresh_markets(),
    )
    assert fake.max_in_flight == 1


@pytest.mark.asyncio
async def test_retry_uses_exponential_backoff(monkeypatch):
    client = client_with(object(), max_retries=4, retry_base_seconds=1.5)
    calls = 0
    delays = []

    async def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise exchange.ccxt.NetworkError("temporary")
        return "ok"

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(exchange.asyncio, "sleep", fake_sleep)
    assert await client._retry(operation, "test") == "ok"
    assert calls == 3
    assert delays == [1.5, 3.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        429,
        418,
    ],
)
async def test_real_ccxt_handle_errors_honors_request_local_retry_after(
    monkeypatch,
    status,
):
    ccxt_client = RequestLocalBinanceUSDM()
    client = client_with(ccxt_client, max_retries=2, retry_base_seconds=1)
    calls = 0
    delays = []
    clock = [100.0]

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            client.exchange.handle_errors(
                status,
                "limited",
                "https://fapi.binance.com/fapi/v1/exchangeInfo",
                "GET",
                {"Retry-After": "7", "X-Request": str(status)},
                "{}",
                {},
                None,
                None,
            )
        return "ok"

    async def fake_sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(exchange.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(exchange.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(exchange.random, "uniform", lambda _a, _b: 0.0)
    assert await client._retry(operation, "limited") == "ok"
    assert delays == [7.0]


@pytest.mark.parametrize("status", [418, 429])
def test_real_ccxt_handle_errors_attaches_exact_response_metadata(status):
    instance = RequestLocalBinanceUSDM()
    with pytest.raises(exchange.ccxt.DDoSProtection) as raised:
        instance.handle_errors(
            status,
            "limited",
            "https://fapi.binance.com/fapi/v1/time",
            "GET",
            {"Retry-After": "17", "X-Request-ID": f"request-{status}"},
            "{}",
            {},
            None,
            None,
        )

    assert raised.value.status_code == status
    assert raised.value.response_headers == {
        "Retry-After": "17",
        "X-Request-ID": f"request-{status}",
    }


def test_real_ccxt_handle_errors_preserves_retry_after_http_date():
    instance = RequestLocalBinanceUSDM()
    with pytest.raises(exchange.ccxt.DDoSProtection) as raised:
        instance.handle_errors(
            429,
            "limited",
            "https://fapi.binance.com/fapi/v1/time",
            "GET",
            {"Retry-After": "Thu, 01 Jan 1970 00:02:00 GMT"},
            "{}",
            {},
            None,
            None,
        )

    assert retry_after_seconds(raised.value, 9, now=60) == 60


@pytest.mark.asyncio
async def test_generic_http_429_ignores_shared_exchange_headers(monkeypatch):
    fake = type(
        "ExchangeWithHeaders",
        (),
        {"last_response_headers": {"Retry-After": "4"}},
    )()
    client = client_with(fake, max_retries=2, retry_base_seconds=1)
    calls = 0
    delays = []
    clock = [100.0]

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise exchange.ccxt.ExchangeError("HTTP 429 Too Many Requests")
        return "ok"

    async def fake_sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(exchange.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(exchange.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(exchange.random, "uniform", lambda _a, _b: 0.0)
    assert await client._retry(operation, "limited") == "ok"
    assert calls == 2
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_418_without_header_uses_conservative_global_cooldown(
    monkeypatch,
):
    client = client_with(object(), max_retries=2, retry_base_seconds=1)
    clock = [500.0]
    delays = []
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            ccxt_client = RequestLocalBinanceUSDM()
            ccxt_client.handle_errors(
                418,
                "banned",
                "https://fapi.binance.com/fapi/v1/time",
                "GET",
                {},
                "{}",
                {},
                None,
                None,
            )
        return "ok"

    async def fake_sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(exchange.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(exchange.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(exchange.random, "uniform", lambda _a, _b: 0.0)
    assert await client._retry(operation, "banned") == "ok"
    assert delays == [60.0]


@pytest.mark.asyncio
async def test_global_cooldown_blocks_concurrent_requests(monkeypatch):
    ccxt_client = RequestLocalBinanceUSDM()
    client = client_with(ccxt_client, max_retries=2, retry_base_seconds=0.01)
    monkeypatch.setattr(exchange.random, "uniform", lambda _a, _b: 0.0)
    first_call = True
    limited = asyncio.Event()
    second_executed = asyncio.Event()

    async def first_operation():
        nonlocal first_call
        if first_call:
            first_call = False
            limited.set()
            ccxt_client.handle_errors(
                429,
                "limited",
                "https://fapi.binance.com/fapi/v1/time",
                "GET",
                {"Retry-After": "0.05"},
                "{}",
                {},
                None,
                None,
            )
        return "first-ok"

    async def second_operation():
        second_executed.set()
        return "second-ok"

    first_task = asyncio.create_task(client._retry(first_operation, "first"))
    await limited.wait()
    while client.blocked_until <= time.monotonic():
        await asyncio.sleep(0)
    second_task = asyncio.create_task(
        client._retry(second_operation, "second")
    )
    await asyncio.sleep(0.01)
    assert not second_executed.is_set()
    assert await second_task == "second-ok"
    assert await first_task == "first-ok"


def test_retry_after_parses_header_and_uses_fallback():
    exc = RuntimeError("limited")
    exc.response_headers = {"retry-after": "2.5"}
    assert retry_after_seconds(exc, 9) == 2.5
    assert retry_after_seconds(RuntimeError("no header"), 9) == 9
    zero = RuntimeError("limited")
    zero.headers = {"Retry-After": "0"}
    assert retry_after_seconds(zero, 9) == 9
    dated = RuntimeError("limited")
    dated.headers = {"Retry-After": "Thu, 01 Jan 1970 00:02:00 GMT"}
    assert retry_after_seconds(dated, 9, now=60) == 60
    invalid = RuntimeError("limited")
    invalid.headers = {"Retry-After": "not-a-date"}
    assert retry_after_seconds(invalid, 9) == 9


@pytest.mark.asyncio
async def test_http_451_fails_fast_without_sleep(monkeypatch):
    client = client_with(object(), max_retries=20)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise exchange.ccxt.ExchangeNotAvailable(
            "binanceusdm GET /fapi/v1/exchangeInfo 451 "
            "Service unavailable from a restricted location"
        )

    async def forbidden_sleep(_):
        pytest.fail("451 must not retry")

    monkeypatch.setattr(exchange.asyncio, "sleep", forbidden_sleep)
    with pytest.raises(BinanceRestrictedLocation, match="HTTP 451"):
        await client._retry(operation, "load_markets")
    assert calls == 1


@pytest.mark.asyncio
async def test_bad_symbol_becomes_market_unavailable():
    client = client_with(object())

    async def operation():
        raise exchange.ccxt.BadSymbol("Invalid symbol, code=-1121")

    with pytest.raises(MarketUnavailable, match="недоступен"):
        await client._retry(operation, "OHLCV OLD/USDT:USDT")


class MarketDataExchange:
    def __init__(self, rows, ticker, error=None):
        self.rows = rows
        self.ticker = ticker
        self.error = error
        self.ticker_calls = 0

    async def fetch_ohlcv(self, symbol, timeframe, limit):
        if self.error:
            raise self.error
        return self.rows

    async def fetch_ticker(self, symbol):
        self.ticker_calls += 1
        return self.ticker


@pytest.mark.asyncio
async def test_fetch_market_returns_only_completed_candles_volume_and_price(
    monkeypatch,
):
    now_ms = 2_000_000
    candle_ms = 15 * 60 * 1000
    rows = [
        [now_ms - 2 * candle_ms, 1, 2, 0.5, 1.5, 10],
        [now_ms - candle_ms, 1.5, 2, 1, 1.8, 11],
        [now_ms, 1.8, 2.1, 1.7, 2, 12],
    ]
    fake = MarketDataExchange(rows, {"quoteVolume": "1234", "last": "2.05"})
    client = client_with(fake)
    client.symbols = ["BTC/USDT:USDT"]
    client.available_symbols = set(client.symbols)
    monkeypatch.setattr(exchange.time, "time", lambda: now_ms / 1000)

    frame, quote_volume, current_price = await client.fetch_market(
        "BTC/USDT:USDT"
    )

    assert len(frame) == 2
    assert list(frame.columns) == [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    assert quote_volume == 1234
    assert current_price == 2.05


@pytest.mark.asyncio
async def test_fetch_market_does_not_query_removed_symbol():
    fake = MarketDataExchange([], {})
    client = client_with(fake)
    client.available_symbols = {"BTC/USDT:USDT"}

    with pytest.raises(MarketUnavailable, match="отсутствует"):
        await client.fetch_market("OLD/USDT:USDT")
    assert fake.ticker_calls == 0


@pytest.mark.parametrize(
    "query",
    ["btc", "BTCUSDT", "btc/usdt:usdt", " BTC "],
)
def test_resolves_manual_symbol_formats(query):
    client = client_with(object())
    client.available_symbols = {"BTC/USDT:USDT", "ETH/USDT:USDT"}
    assert client.resolve_symbol(query) == "BTC/USDT:USDT"


def test_rejects_unknown_manual_symbol():
    client = client_with(object())
    client.available_symbols = {"BTC/USDT:USDT"}
    assert client.resolve_symbol("UNKNOWN") is None
