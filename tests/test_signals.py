import json
import time
from dataclasses import replace

import pandas as pd
import pytest

from signals import SignalStore
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
async def test_old_state_file_is_migrated_with_new_tracking_fields(tmp_path):
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
    signal = store.active["X/USDT:USDT"]
    assert signal.signal_id == ""
    assert signal.telegram_message_id is None
    assert not signal.tp1_hit
    assert store.pending_events == []


@pytest.mark.asyncio
async def test_pending_only_old_state_blocks_old_candle_after_upgrade(
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
    assert store.last_signal_candles["X/USDT:USDT"] == 3000
    assert not await store.add(evaluation(), "duplicate-old-candle")

    await store.acknowledge_event("old:SL")
    new_evaluation = replace(evaluation(), candle_timestamp=4000)
    assert await store.add(new_evaluation, "new-candle")
