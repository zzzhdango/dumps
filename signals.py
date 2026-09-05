from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from strategy import StrategyEvaluation


@dataclass(slots=True)
class ActiveSignal:
    symbol: str
    candle_timestamp: int
    entry: float
    tp1: float
    tp2: float
    tp3: float
    sl: float
    created_at: int
    expires_at: int = 0
    signal_id: str = ""
    telegram_message_id: int | None = None
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False


@dataclass(slots=True)
class SignalEvent:
    event_id: str
    kind: str
    symbol: str
    signal_id: str
    entry: float
    level_price: float
    event_timestamp: int
    telegram_message_id: int | None = None


class SignalStore:
    def __init__(self, path: str, valid_hours: float = 6.0):
        self.path = Path(path)
        self.valid_ms = int(valid_hours * 60 * 60 * 1000)
        self._lock = asyncio.Lock()
        self.active: dict[str, ActiveSignal] = {}
        self.pending_events: list[SignalEvent] = []
        self.last_signal_candles: dict[str, int] = {}

    async def load(self) -> None:
        async with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                restored = {}
                for key, value in data.get("active", {}).items():
                    item = dict(value)
                    item.setdefault("expires_at", int(item.get("created_at", 0)) + self.valid_ms)
                    item.setdefault("signal_id", "")
                    item.setdefault("telegram_message_id", None)
                    item.setdefault("tp1_hit", False)
                    item.setdefault("tp2_hit", False)
                    item.setdefault("tp3_hit", False)
                    restored[key] = ActiveSignal(**item)
                self.active = restored
                self.pending_events = [
                    SignalEvent(**item) for item in data.get("pending_events", [])
                ]
                self.last_signal_candles = {
                    symbol: int(timestamp)
                    for symbol, timestamp in data.get(
                        "last_signal_candles",
                        {},
                    ).items()
                }
                for symbol, signal in self.active.items():
                    previous = self.last_signal_candles.get(symbol, 0)
                    self.last_signal_candles[symbol] = max(
                        previous,
                        signal.candle_timestamp,
                    )
                # Старые state-файлы могли содержать только неотправленный
                # TP3/SL без active и без last_signal_candles. В таком случае
                # время события служит консервативной границей: сканер
                # дождётся свечи, начавшейся после закрытия прошлой сделки.
                for event in self.pending_events:
                    previous = self.last_signal_candles.get(event.symbol, 0)
                    self.last_signal_candles[event.symbol] = max(
                        previous,
                        event.event_timestamp,
                    )
            except (OSError, ValueError, TypeError):
                self.active = {}
                self.pending_events = []
                self.last_signal_candles = {}

    def is_active(self, symbol: str) -> bool:
        return symbol in self.active

    async def prune_active_symbols(
        self,
        available_symbols: set[str],
    ) -> set[str]:
        async with self._lock:
            removed = set(self.active) - available_symbols
            if not removed:
                return set()
            for symbol in removed:
                del self.active[symbol]
            await self._save_unlocked()
            return removed

    async def add(
        self,
        evaluation: StrategyEvaluation,
        signal_id: str = "",
        created_at_ms: int | None = None,
    ) -> bool:
        if not evaluation.passed or evaluation.levels is None:
            return False
        async with self._lock:
            if evaluation.symbol in self.active:
                return False
            if evaluation.candle_timestamp <= self.last_signal_candles.get(
                evaluation.symbol,
                -1,
            ):
                return False
            lv = evaluation.levels
            now_ms = created_at_ms if created_at_ms is not None else int(time.time() * 1000)
            self.active[evaluation.symbol] = ActiveSignal(evaluation.symbol, evaluation.candle_timestamp,
                lv.entry, lv.tp1, lv.tp2, lv.tp3, lv.sl, now_ms, now_ms + self.valid_ms,
                signal_id=signal_id)
            self.last_signal_candles[evaluation.symbol] = (
                evaluation.candle_timestamp
            )
            await self._save_unlocked()
            return True

    async def set_message_id(self, symbol: str, message_id: int) -> None:
        async with self._lock:
            sig = self.active.get(symbol)
            if sig:
                sig.telegram_message_id = message_id
                await self._save_unlocked()

    async def acknowledge_event(self, event_id: str) -> None:
        async with self._lock:
            before = len(self.pending_events)
            self.pending_events = [
                event for event in self.pending_events if event.event_id != event_id
            ]
            if len(self.pending_events) != before:
                await self._save_unlocked()

    def _pending_for(self, symbol: str) -> list[SignalEvent]:
        return [event for event in self.pending_events if event.symbol == symbol]

    def _queue_event(
        self,
        sig: ActiveSignal,
        kind: str,
        level_price: float,
        event_timestamp: int,
    ) -> None:
        event_id = f"{sig.signal_id or sig.symbol}:{sig.created_at}:{kind}"
        if any(event.event_id == event_id for event in self.pending_events):
            return
        self.pending_events.append(SignalEvent(
            event_id=event_id,
            kind=kind,
            symbol=sig.symbol,
            signal_id=sig.signal_id,
            entry=sig.entry,
            level_price=level_price,
            event_timestamp=event_timestamp,
            telegram_message_id=sig.telegram_message_id,
        ))

    async def update_from_candles(
        self,
        symbol: str,
        candles: Any,
        current_price: float | None = None,
    ) -> list[SignalEvent]:
        async with self._lock:
            sig = self.active.get(symbol)
            if not sig:
                return self._pending_for(symbol)
            if sig.expires_at and int(time.time() * 1000) >= sig.expires_at:
                del self.active[symbol]
                await self._save_unlocked()
                return self._pending_for(symbol)
            later = candles[candles["timestamp"] > sig.candle_timestamp]
            changed = False
            for row in later.itertuples(index=False):
                timestamp = int(time.time() * 1000)
                # Консервативно: если на одной свече достигнуты SL и TP1, первым считается SL.
                if float(row.high) >= sig.sl:
                    self._queue_event(sig, "SL", sig.sl, timestamp)
                    del self.active[symbol]
                    changed = True
                    break
                low = float(row.low)
                if low <= sig.tp1 and not sig.tp1_hit:
                    sig.tp1_hit = True
                    self._queue_event(sig, "TP1", sig.tp1, timestamp)
                    changed = True
                if low <= sig.tp2 and not sig.tp2_hit:
                    sig.tp2_hit = True
                    self._queue_event(sig, "TP2", sig.tp2, timestamp)
                    changed = True
                if low <= sig.tp3 and not sig.tp3_hit:
                    sig.tp3_hit = True
                    self._queue_event(sig, "TP3", sig.tp3, timestamp)
                    del self.active[symbol]
                    changed = True
                    break
            sig = self.active.get(symbol)
            live_price = float(current_price or 0)
            if sig and live_price > 0:
                now_ms = int(time.time() * 1000)
                if live_price >= sig.sl:
                    self._queue_event(sig, "SL", sig.sl, now_ms)
                    del self.active[symbol]
                    changed = True
                else:
                    if live_price <= sig.tp1 and not sig.tp1_hit:
                        sig.tp1_hit = True
                        self._queue_event(sig, "TP1", sig.tp1, now_ms)
                        changed = True
                    if live_price <= sig.tp2 and not sig.tp2_hit:
                        sig.tp2_hit = True
                        self._queue_event(sig, "TP2", sig.tp2, now_ms)
                        changed = True
                    if live_price <= sig.tp3 and not sig.tp3_hit:
                        sig.tp3_hit = True
                        self._queue_event(sig, "TP3", sig.tp3, now_ms)
                        del self.active[symbol]
                        changed = True
            if changed:
                await self._save_unlocked()
            return self._pending_for(symbol)

    async def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active": {k: asdict(v) for k, v in self.active.items()},
            "pending_events": [asdict(event) for event in self.pending_events],
            "last_signal_candles": self.last_signal_candles,
        }
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)
