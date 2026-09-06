import asyncio

import pytest

import main
from config import Config
from exchange import BinanceRestrictedLocation, MarketUnavailable
from runtime_lock import AlreadyRunningError, RuntimeSingletonLock
from signals import (
    CURRENT_PROVIDER,
    ActiveSignal,
    SignalEvent,
    SignalStore,
)
from strategy import SignalLevels, StrategyEvaluation


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return type("Sent", (), {"message_id": 1})()


def passing_evaluation() -> StrategyEvaluation:
    return StrategyEvaluation(
        "BTC/USDT:USDT",
        True,
        1000,
        {},
        {},
        SignalLevels(100, 95, 90, 85, 110, 3, None, None, None),
        ("ok",),
    )


@pytest.mark.asyncio
async def test_monitor_delivers_pending_then_never_fetches_removed_symbol(
    tmp_path,
    monkeypatch,
):
    symbol = "OLD/USDT:USDT"
    store = SignalStore(str(tmp_path / "state.json"))
    store.active[symbol] = ActiveSignal(
        symbol, 1, 100, 95, 90, 85, 110, 1
    )
    store.pending_events = [
        SignalEvent("old:TP1", "TP1", symbol, "old", 100, 95, 2)
    ]

    class Client:
        available_symbols = {"BTC/USDT:USDT"}
        fetch_calls = []

        async def fetch_market(self, requested):
            self.fetch_calls.append(requested)
            raise AssertionError("removed symbol must not be requested")

    async def stop_after_cycle(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_cycle)
    client = Client()
    bot = FakeBot()

    with pytest.raises(asyncio.CancelledError):
        await main.monitor_active_signals(
            Config(),
            client,
            store,
            bot,
            {},
            {},
        )

    assert len(bot.messages) == 1
    assert store.pending_events == []
    assert not store.is_active(symbol)
    assert client.fetch_calls == []


@pytest.mark.asyncio
async def test_scan_uses_symbol_snapshot_then_refreshes_and_prunes(
    tmp_path,
    monkeypatch,
):
    old = "OLD/USDT:USDT"
    new = "NEW/USDT:USDT"
    store = SignalStore(str(tmp_path / "state.json"))
    store.active[old] = ActiveSignal(old, 1, 100, 95, 90, 85, 110, 1)

    class Client:
        def __init__(self):
            self.symbols = [old]
            self.available_symbols = {old}
            self.fetch_calls = []
            self.refresh_calls = 0

        async def fetch_market(self, symbol):
            self.fetch_calls.append(symbol)
            raise MarketUnavailable("delisted")

        async def refresh_markets(self):
            self.refresh_calls += 1
            self.symbols = [new]
            self.available_symbols = {new}
            return {new}, {old}

    async def stop_after_cycle(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_cycle)
    client = Client()

    with pytest.raises(asyncio.CancelledError):
        await main.scan_forever(
            Config(),
            client,
            store,
            FakeBot(),
            {},
            {},
        )

    assert client.fetch_calls == [old]
    assert client.refresh_calls == 1
    assert not store.is_active(old)


@pytest.mark.asyncio
async def test_scanner_451_propagates_and_cancels_other_market_work(
    tmp_path,
):
    second_started = asyncio.Event()
    second_cancelled = asyncio.Event()

    class Client:
        symbols = ["A/USDT:USDT", "B/USDT:USDT"]
        available_symbols = set(symbols)
        refresh_calls = 0

        async def fetch_market(self, symbol):
            if symbol.startswith("A/"):
                await second_started.wait()
                raise BinanceRestrictedLocation("HTTP 451")
            second_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                second_cancelled.set()
                raise

        async def refresh_markets(self):
            self.refresh_calls += 1
            return set(), set()

    runtime = {}
    client = Client()
    with pytest.raises(BinanceRestrictedLocation):
        await main.scan_forever(
            Config(),
            client,
            SignalStore(str(tmp_path / "state.json")),
            FakeBot(),
            runtime,
            {},
        )

    assert second_cancelled.is_set()
    assert client.refresh_calls == 0
    assert runtime["ready"] is False
    assert "451" in runtime["fatal_error"]


def test_readiness_requires_fresh_scanner_monitor_and_catalog_cycles():
    cfg = Config()
    ready, payload = main.readiness_status({}, cfg, now=1000)
    assert not ready
    assert payload["status"] == "not_ready"

    runtime = {
        "market_count": 10,
        "catalog_success_at": 900,
        "scanner_success_at": 900,
        "monitor_success_at": 950,
    }
    assert main.readiness_status(runtime, cfg, now=1000)[0]
    assert not main.readiness_status(runtime, cfg, now=2000)[0]

    runtime["fatal_error"] = "HTTP 451"
    ready, payload = main.readiness_status(runtime, cfg, now=1000)
    assert not ready
    assert payload["status"] == "blocked"


@pytest.mark.asyncio
async def test_fatal_watcher_propagates_restricted_location():
    runtime = {"fatal_event": asyncio.Event()}
    task = asyncio.create_task(main.watch_fatal(runtime))
    main.mark_fatal(runtime, BinanceRestrictedLocation("HTTP 451"))
    with pytest.raises(BinanceRestrictedLocation, match="451"):
        await task


@pytest.mark.asyncio
async def test_legacy_pending_delivery_is_explicitly_archival():
    bot = FakeBot()
    event = SignalEvent(
        "legacy:SL",
        "SL",
        "BTC/USDT:USDT",
        "legacy",
        100,
        110,
        1000,
        provider="bingx",
    )

    await main.send_signal_event(bot, "chat", event)

    text = bot.messages[0][1]
    assert "Архивное уведомление провайдера bingx" in text
    assert "не новый сигнал Binance Futures" in text


@pytest.mark.asyncio
async def test_initial_signal_outbox_survives_send_failure_and_restart(
    tmp_path,
):
    path = tmp_path / "state.json"
    store = SignalStore(str(path))
    assert await store.add(
        passing_evaluation(),
        "stable-id",
        1234,
        "initial signal text",
    )

    class FailingBot:
        async def send_message(self, *_args, **_kwargs):
            raise RuntimeError("Telegram unavailable")

    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        await main.deliver_signal_events(
            FailingBot(),
            Config(telegram_chat_id="chat"),
            store,
            store.pending_for("BTC/USDT:USDT"),
        )

    restored = SignalStore(str(path))
    await restored.load()
    assert restored.is_active("BTC/USDT:USDT")
    assert len(restored.pending_events) == 1
    event = restored.pending_events[0]
    assert event.kind == "INITIAL"
    assert event.event_id == "stable-id:1234:INITIAL"
    assert event.provider == CURRENT_PROVIDER
    assert event.text == "initial signal text"


@pytest.mark.asyncio
async def test_initial_outbox_cancel_and_ack_failure_remain_pending(tmp_path):
    store = SignalStore(str(tmp_path / "state.json"))
    await store.add(passing_evaluation(), "stable-id", 1234, "message")

    class CancelBot:
        async def send_message(self, *_args, **_kwargs):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await main.deliver_signal_events(
            CancelBot(),
            Config(telegram_chat_id="chat"),
            store,
            list(store.pending_events),
        )
    assert len(store.pending_events) == 1

    bot = FakeBot()

    async def fail_ack(*_args, **_kwargs):
        raise OSError("disk failure")

    store.acknowledge_event = fail_ack
    with pytest.raises(OSError, match="disk failure"):
        await main.deliver_signal_events(
            bot,
            Config(telegram_chat_id="chat"),
            store,
            list(store.pending_events),
        )
    assert len(bot.messages) == 1
    assert len(store.pending_events) == 1


@pytest.mark.asyncio
async def test_initial_outbox_success_sets_reply_id_atomically(tmp_path):
    path = tmp_path / "state.json"
    store = SignalStore(str(path))
    await store.add(passing_evaluation(), "stable-id", 1234, "message")
    await main.deliver_signal_events(
        FakeBot(),
        Config(telegram_chat_id="chat"),
        store,
        list(store.pending_events),
    )
    restored = SignalStore(str(path))
    await restored.load()
    assert restored.pending_events == []
    assert restored.active["BTC/USDT:USDT"].telegram_message_id == 1


def test_runtime_singleton_lock_rejects_second_process(tmp_path):
    state = str(tmp_path / "signals_state.json")
    first = RuntimeSingletonLock(state)
    second = RuntimeSingletonLock(state)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_runtime_secret_validation_rejects_examples():
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        main.validate_runtime_secrets(
            Config(
                telegram_bot_token="123456:replace_me",
                telegram_chat_id="-100999",
            )
        )


@pytest.mark.asyncio
async def test_supervisor_propagates_failure_and_cancels_other_workers():
    cancelled = asyncio.Event()

    async def failing():
        await asyncio.sleep(0)
        raise BinanceRestrictedLocation("HTTP 451")

    async def waiting():
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    tasks = {
        "scanner": asyncio.create_task(failing()),
        "monitor": asyncio.create_task(waiting()),
    }
    with pytest.raises(BinanceRestrictedLocation):
        await main.supervise_tasks(tasks)
    assert cancelled.is_set()
    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
        main.validate_runtime_secrets(
            Config(
                telegram_bot_token="123456:real",
                telegram_chat_id="-1001234567890",
            )
        )
