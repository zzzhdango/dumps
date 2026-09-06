import json
import time
from dataclasses import replace

import pandas as pd
import pytest

from signals import (
    CURRENT_PROVIDER,
    LEGACY_PROVIDER,
    STATE_SCHEMA_VERSION,
    SignalStore,
    StateValidationError,
)
from strategy import SignalLevels, StrategyEvaluation


def evaluation() -> StrategyEvaluation:
    levels = SignalLevels(100, 94.5, 90, 85, 111.25, 3, None, None, None)
    return StrategyEvaluation("X/USDT:USDT", True, 1000, {}, {}, levels, ("ok",))


def candle(timestamp: int, high: float = 101, low: float = 99) -> pd.DataFrame:
    return pd.DataFrame([{
        "timestamp": timestamp,
        "open": 100,
        "high": high,
        "low": low,
        "close": 100,
        "volume": 1,
    }])


@pytest.mark.asyncio
async def test_dedup_persistence_and_conservative_close(tmp_path):
    path = tmp_path / "state.json"
    store = SignalStore(str(path))
    ev = evaluation()
    assert await store.add(ev, "20260905-X-1200")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == STATE_SCHEMA_VERSION
    assert payload["provider"] == CURRENT_PROVIDER
    assert payload["active"][ev.symbol]["provider"] == CURRENT_PROVIDER
    assert not await store.add(ev)
    await store.set_message_id(ev.symbol, 777)
    restored = SignalStore(str(path))
    await restored.load()
    assert restored.is_active(ev.symbol)
    assert restored.active[ev.symbol].signal_id == "20260905-X-1200"
    assert restored.active[ev.symbol].telegram_message_id == 777
    events = await restored.update_from_candles(ev.symbol, candle(2000, high=112, low=84))
    assert [event.kind for event in events] == ["SL"]
    assert events[0].telegram_message_id == 777
    assert not restored.is_active(ev.symbol)


@pytest.mark.asyncio
async def test_closed_signal_cannot_repeat_on_same_candle_after_restart(
    tmp_path,
):
    path = tmp_path / "state.json"
    store = SignalStore(str(path))
    ev = evaluation()
    assert await store.add(ev, "same-candle")
    events = await store.update_from_candles(
        ev.symbol,
        candle(2000, low=84),
    )
    assert [event.kind for event in events] == ["TP1", "TP2", "TP3"]
    assert not await store.add(ev, "duplicate")

    restored = SignalStore(str(path))
    await restored.load()
    assert not await restored.add(ev, "duplicate-after-restart")
    for event in list(restored.pending_events):
        await restored.acknowledge_event(event.event_id)
    next_candle = replace(
        ev,
        candle_timestamp=restored.last_signal_candles[ev.symbol] + 1,
    )
    assert await restored.add(next_candle, "next-candle")


@pytest.mark.asyncio
async def test_tp_events_are_sequential_persistent_and_not_duplicated(tmp_path):
    path = tmp_path / "state.json"
    store = SignalStore(str(path))
    ev = evaluation()
    assert await store.add(ev, "signal-1")
    await store.set_message_id(ev.symbol, 888)

    tp1_events = await store.update_from_candles(ev.symbol, candle(2000, low=94))
    assert [event.kind for event in tp1_events] == ["TP1"]
    assert store.is_active(ev.symbol)

    retry_events = await store.update_from_candles(ev.symbol, candle(2000, low=94))
    assert [event.kind for event in retry_events] == ["TP1"]
    await store.acknowledge_event(tp1_events[0].event_id)
    assert await store.update_from_candles(ev.symbol, candle(2000, low=94)) == []

    restored = SignalStore(str(path))
    await restored.load()
    assert restored.active[ev.symbol].tp1_hit
    assert restored.active[ev.symbol].telegram_message_id == 888

    tp2_events = await restored.update_from_candles(ev.symbol, candle(3000, low=89))
    assert [event.kind for event in tp2_events] == ["TP2"]
    await restored.acknowledge_event(tp2_events[0].event_id)
    assert restored.is_active(ev.symbol)

    tp3_events = await restored.update_from_candles(ev.symbol, candle(4000, low=84))
    assert [event.kind for event in tp3_events] == ["TP3"]
    assert not restored.is_active(ev.symbol)


@pytest.mark.asyncio
async def test_one_candle_can_complete_all_take_profits(tmp_path):
    store = SignalStore(str(tmp_path / "state.json"))
    ev = evaluation()
    assert await store.add(ev, "signal-2")
    events = await store.update_from_candles(ev.symbol, candle(2000, low=84))
    assert [event.kind for event in events] == ["TP1", "TP2", "TP3"]
    assert not store.is_active(ev.symbol)


@pytest.mark.asyncio
async def test_stop_after_tp1_closes_remaining_signal(tmp_path):
    store = SignalStore(str(tmp_path / "state.json"))
    ev = evaluation()
    assert await store.add(ev, "signal-3")
    events = await store.update_from_candles(ev.symbol, candle(2000, low=94))
    await store.acknowledge_event(events[0].event_id)
    events = await store.update_from_candles(ev.symbol, candle(3000, high=112))
    assert [event.kind for event in events] == ["SL"]
    assert not store.is_active(ev.symbol)


@pytest.mark.asyncio
async def test_live_ticker_price_triggers_take_profit_before_next_closed_candle(tmp_path):
    store = SignalStore(str(tmp_path / "state.json"))
    ev = evaluation()
    assert await store.add(ev, "signal-live")
    events = await store.update_from_candles(
        ev.symbol,
        candle(1000),
        current_price=89,
    )
    assert [event.kind for event in events] == ["TP1", "TP2"]
    assert store.is_active(ev.symbol)


@pytest.mark.asyncio
async def test_expired_signal_releases_deduplication_without_notification(tmp_path):
    store = SignalStore(str(tmp_path / "state.json"), valid_hours=6)
    ev = evaluation()
    assert await store.add(ev)
    store.active[ev.symbol].expires_at = 1
    assert await store.update_from_candles(ev.symbol, candle(2000)) == []
    assert not store.is_active(ev.symbol)


@pytest.mark.asyncio
async def test_unversioned_bingx_active_is_quarantined_not_resumed(tmp_path):
    path = tmp_path / "state.json"
    now_ms = int(time.time() * 1000)
    path.write_text(json.dumps({
        "active": {
            "X/USDT:USDT": {
                "symbol": "X/USDT:USDT",
                "candle_timestamp": 1000,
                "entry": 100,
                "tp1": 94.5,
                "tp2": 90,
                "tp3": 85,
                "sl": 111.25,
                "created_at": now_ms,
            }
        }
    }), encoding="utf-8")

    store = SignalStore(str(path))
    await store.load()
    assert store.active == {}
    assert store.legacy_quarantine["X/USDT:USDT"]["provider"] == LEGACY_PROVIDER
    assert store.pending_events == []
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == STATE_SCHEMA_VERSION
    assert migrated["provider"] == CURRENT_PROVIDER
    assert migrated["active"] == {}
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_unknown_state_schema_cannot_resume_active_signal(tmp_path):
    path = tmp_path / "state.json"
    item = {
        "symbol": "X/USDT:USDT",
        "candle_timestamp": 1000,
        "entry": 100,
        "tp1": 94.5,
        "tp2": 90,
        "tp3": 85,
        "sl": 111.25,
        "created_at": 1000,
        "provider": CURRENT_PROVIDER,
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": STATE_SCHEMA_VERSION + 1,
                "provider": CURRENT_PROVIDER,
                "active": {"X/USDT:USDT": item},
            }
        ),
        encoding="utf-8",
    )

    store = SignalStore(str(path))
    await store.load()

    assert store.active == {}
    assert (
        store.legacy_quarantine["X/USDT:USDT"]["reason"]
        == "unsupported_schema"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("active", {"X/USDT:USDT": {"symbol": "X/USDT:USDT"}}),
        ("pending_events", [{"event_id": "incomplete"}]),
        ("active", []),
        ("pending_events", {}),
        ("last_signal_candles", []),
    ],
)
async def test_current_v2_malformed_state_fails_startup(tmp_path, field, value):
    path = tmp_path / "state.json"
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "provider": CURRENT_PROVIDER,
        "active": {},
        "pending_events": [],
        "last_signal_candles": {},
        "legacy_quarantine": {},
    }
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = SignalStore(str(path))
    with pytest.raises(StateValidationError, match="State v2"):
        await store.load()

    assert store.active == {}
    assert store.pending_events == []


@pytest.mark.asyncio
async def test_legacy_malformed_entries_are_quarantined_individually(tmp_path):
    path = tmp_path / "state.json"
    valid_pending = {
        "event_id": "old:SL",
        "kind": "SL",
        "symbol": "X/USDT:USDT",
        "signal_id": "old",
        "entry": 100,
        "level_price": 110,
        "event_timestamp": 2000,
    }
    path.write_text(
        json.dumps(
            {
                "active": {
                    "BROKEN/USDT:USDT": {"symbol": "BROKEN/USDT:USDT"},
                },
                "pending_events": [valid_pending, {"event_id": "broken"}],
                "last_signal_candles": [],
            }
        ),
        encoding="utf-8",
    )

    store = SignalStore(str(path))
    await store.load()

    assert [event.event_id for event in store.pending_events] == ["old:SL"]
    assert store.pending_events[0].provider.startswith("bingx")
    assert "malformed_active:BROKEN/USDT:USDT" in store.legacy_quarantine
    assert "malformed_pending:1" in store.legacy_quarantine
    assert "malformed_container:last_signal_candles" in store.legacy_quarantine


@pytest.mark.asyncio
async def test_unsupported_schema_preserves_valid_pending_and_quarantines_bad(
    tmp_path,
):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "provider": CURRENT_PROVIDER,
                "active": {},
                "pending_events": [
                    {
                        "event_id": "future:TP1",
                        "kind": "TP1",
                        "symbol": "X/USDT:USDT",
                        "signal_id": "future",
                        "entry": 100,
                        "level_price": 95,
                        "event_timestamp": 3000,
                    },
                    {"event_id": "broken"},
                ],
            }
        ),
        encoding="utf-8",
    )

    store = SignalStore(str(path))
    await store.load()

    assert [event.event_id for event in store.pending_events] == ["future:TP1"]
    assert store.pending_events[0].provider.startswith("unsupported:")
    assert "malformed_pending:1" in store.legacy_quarantine


@pytest.mark.asyncio
async def test_legacy_pending_is_preserved_but_does_not_block_binance_dedup(
    tmp_path,
):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "active": {},
                "pending_events": [
                    {
                        "event_id": "old:SL",
                        "kind": "SL",
                        "symbol": "X/USDT:USDT",
                        "signal_id": "old",
                        "entry": 100,
                        "level_price": 111.25,
                        "event_timestamp": 3000,
                        "telegram_message_id": 777,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = SignalStore(str(path))
    await store.load()
    assert store.last_signal_candles == {}
    assert store.pending_events[0].provider == LEGACY_PROVIDER
    assert await store.add(evaluation(), "new-binance-signal")

    await store.acknowledge_event("old:SL")
    assert store.is_active(evaluation().symbol)


@pytest.mark.asyncio
async def test_old_bingx_unified_state_is_compatible_and_pruned(
    tmp_path,
):
    path = tmp_path / "state.json"
    store = SignalStore(str(path))
    crypto = evaluation()
    removed_market = replace(
        evaluation(),
        symbol="OLD/USDT:USDT",
    )
    assert await store.add(crypto, "crypto-signal")
    assert await store.add(removed_market, "old-market-signal")

    events = await store.update_from_candles(
        removed_market.symbol,
        candle(2000, low=94),
    )
    assert [event.kind for event in events] == ["TP1"]

    removed = await store.prune_active_symbols({crypto.symbol})
    assert removed == {removed_market.symbol}
    assert store.is_active(crypto.symbol)
    assert not store.is_active(removed_market.symbol)
    assert [event.kind for event in store.pending_events] == ["TP1"]
    assert store.pending_events[0].symbol == removed_market.symbol

    restored = SignalStore(str(path))
    await restored.load()
    assert restored.is_active(crypto.symbol)
    assert not restored.is_active(removed_market.symbol)
    assert [event.kind for event in restored.pending_events] == ["TP1"]
    assert restored.pending_events[0].symbol == removed_market.symbol
