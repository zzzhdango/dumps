from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from state_schema import (
    CURRENT_PROVIDER,
    STATE_SCHEMA_VERSION,
    StateSchemaError,
    validate_active_record,
    validate_current_state_data,
    validate_event_record,
)
from strategy import StrategyEvaluation

LEGACY_PROVIDER = "bingx"
log = logging.getLogger(__name__)


class StateValidationError(RuntimeError):
    """Persistent state is unsafe to load without operator intervention."""


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
    provider: str = CURRENT_PROVIDER


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
    provider: str = CURRENT_PROVIDER
    text: str | None = None


class SignalStore:
    def __init__(self, path: str, valid_hours: float = 6.0):
        self.path = Path(path)
        self.valid_ms = int(valid_hours * 60 * 60 * 1000)
        self._lock = asyncio.Lock()
        self.active: dict[str, ActiveSignal] = {}
        self.pending_events: list[SignalEvent] = []
        self.last_signal_candles: dict[str, int] = {}
        self.legacy_quarantine: dict[str, Any] = {}

    async def load(self) -> None:
        async with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StateValidationError(
                    f"State {self.path} не читается или содержит неверный JSON"
                ) from exc
            if not isinstance(data, dict):
                raise StateValidationError("Корень state должен быть JSON object")

            schema_version = data.get("schema_version")
            state_provider = data.get("provider")
            is_legacy = schema_version is None
            resume_current = (
                schema_version == STATE_SCHEMA_VERSION
                and state_provider == CURRENT_PROVIDER
            )
            if resume_current:
                try:
                    validate_current_state_data(data)
                except StateSchemaError as exc:
                    raise StateValidationError(f"State v2: {exc}") from exc
            source_provider = (
                LEGACY_PROVIDER
                if is_legacy
                else state_provider or f"unknown-schema-{schema_version}"
            )
            archive_provider = (
                source_provider
                if is_legacy
                else f"unsupported:{source_provider}:schema-{schema_version}"
            )

            def container(name: str, expected: type, default: Any) -> Any:
                value = data.get(name, default)
                if isinstance(value, expected):
                    return value
                if resume_current:
                    raise StateValidationError(
                        f"State v2: {name} должен иметь тип {expected.__name__}"
                    )
                quarantined[f"malformed_container:{name}"] = {
                    "provider": archive_provider,
                    "reason": "malformed_container",
                    "value": value,
                }
                return default

            restored: dict[str, ActiveSignal] = {}
            pending_events: list[SignalEvent] = []
            quarantined_raw = data.get("legacy_quarantine", {})
            if isinstance(quarantined_raw, dict):
                quarantined: dict[str, Any] = dict(quarantined_raw)
            elif resume_current:
                raise StateValidationError(
                    "State v2: legacy_quarantine должен иметь тип dict"
                )
            else:
                quarantined = {
                    "malformed_container:legacy_quarantine": {
                        "provider": archive_provider,
                        "reason": "malformed_container",
                        "value": quarantined_raw,
                    }
                }

            active_items = container("active", dict, {})
            for key, value in active_items.items():
                try:
                    if not isinstance(key, str) or not isinstance(value, dict):
                        raise TypeError("active entry должен быть object")
                    item = dict(value)
                    item_provider = item.get("provider", source_provider)
                    if not resume_current or item_provider != CURRENT_PROVIDER:
                        # Validate enough structure to distinguish a valid
                        # legacy signal from a damaged record.
                        candidate = dict(item)
                        candidate.setdefault(
                            "expires_at",
                            int(candidate.get("created_at", 0)) + self.valid_ms,
                        )
                        candidate.setdefault("signal_id", "")
                        candidate.setdefault("telegram_message_id", None)
                        candidate.setdefault("tp1_hit", False)
                        candidate.setdefault("tp2_hit", False)
                        candidate.setdefault("tp3_hit", False)
                        candidate.setdefault("provider", item_provider or archive_provider)
                        validate_active_record(
                            key,
                            candidate,
                            require_current_provider=False,
                        )
                        quarantined[key] = {
                            "provider": item_provider or "unknown",
                            "reason": (
                                "provider_migration"
                                if item_provider != CURRENT_PROVIDER
                                else "unsupported_schema"
                            ),
                            "signal": item,
                        }
                        continue
                    item.setdefault(
                        "expires_at",
                        int(item.get("created_at", 0)) + self.valid_ms,
                    )
                    item.setdefault("signal_id", "")
                    item.setdefault("telegram_message_id", None)
                    item.setdefault("tp1_hit", False)
                    item.setdefault("tp2_hit", False)
                    item.setdefault("tp3_hit", False)
                    item["provider"] = CURRENT_PROVIDER
                    signal = ActiveSignal(**item)
                    restored[key] = signal
                except (KeyError, TypeError, ValueError, StateSchemaError) as exc:
                    if resume_current:
                        raise StateValidationError(
                            f"State v2: malformed active entry {key!r}: {exc}"
                        ) from exc
                    quarantine_key = f"malformed_active:{key}"
                    quarantined[quarantine_key] = {
                        "provider": archive_provider,
                        "reason": "malformed_entry",
                        "signal": value,
                        "error": str(exc),
                    }
                    log.warning("Legacy state active %r помещён в quarantine: %s", key, exc)

            pending_items = container("pending_events", list, [])
            for index, value in enumerate(pending_items):
                try:
                    if not isinstance(value, dict):
                        raise TypeError("pending entry должен быть object")
                    item = dict(value)
                    item.setdefault(
                        "provider",
                        source_provider if resume_current else archive_provider,
                    )
                    item.setdefault("telegram_message_id", None)
                    item.setdefault("text", None)
                    event = SignalEvent(**item)
                    validate_event_record(item, index)
                    pending_events.append(event)
                except (KeyError, TypeError, ValueError, StateSchemaError) as exc:
                    if resume_current:
                        raise StateValidationError(
                            f"State v2: malformed pending entry {index}: {exc}"
                        ) from exc
                    quarantined[f"malformed_pending:{index}"] = {
                        "provider": archive_provider,
                        "reason": "malformed_entry",
                        "event": value,
                        "error": str(exc),
                    }
                    log.warning(
                        "Legacy state pending[%d] помещён в quarantine: %s",
                        index,
                        exc,
                    )

            dedup_items = container("last_signal_candles", dict, {})
            if resume_current:
                try:
                    if any(
                        not isinstance(symbol, str)
                        or not isinstance(timestamp, int)
                        or isinstance(timestamp, bool)
                        for symbol, timestamp in dedup_items.items()
                    ):
                        raise ValueError(
                            "dedup keys должны быть strings, values — integers"
                        )
                    self.last_signal_candles = {
                        symbol: timestamp
                        for symbol, timestamp in dedup_items.items()
                    }
                except (TypeError, ValueError) as exc:
                    raise StateValidationError(
                        f"State v2: malformed last_signal_candles: {exc}"
                    ) from exc
            else:
                self.last_signal_candles = {}

            self.active = restored
            self.pending_events = pending_events
            self.legacy_quarantine = quarantined
            for symbol, signal in self.active.items():
                previous = self.last_signal_candles.get(symbol, 0)
                self.last_signal_candles[symbol] = max(
                    previous,
                    signal.candle_timestamp,
                )
            # Pending events from untrusted schemas/providers must be delivered
            # but must never influence Binance deduplication.
            for event in self.pending_events:
                if event.provider != CURRENT_PROVIDER or not resume_current:
                    continue
                previous = self.last_signal_candles.get(event.symbol, 0)
                self.last_signal_candles[event.symbol] = max(
                    previous,
                    event.event_timestamp,
                )
            if not resume_current:
                await self._save_unlocked()

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
        initial_message: str | None = None,
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
            if initial_message:
                event_id = (
                    f"{signal_id or evaluation.symbol}:{now_ms}:INITIAL"
                )
                self.pending_events.append(
                    SignalEvent(
                        event_id=event_id,
                        kind="INITIAL",
                        symbol=evaluation.symbol,
                        signal_id=signal_id,
                        entry=lv.entry,
                        level_price=lv.entry,
                        event_timestamp=now_ms,
                        text=initial_message,
                    )
                )
            await self._save_unlocked()
            return True

    async def set_message_id(self, symbol: str, message_id: int) -> None:
        async with self._lock:
            sig = self.active.get(symbol)
            if sig:
                sig.telegram_message_id = message_id
                await self._save_unlocked()

    async def acknowledge_event(
        self,
        event_id: str,
        telegram_message_id: int | None = None,
    ) -> None:
        async with self._lock:
            delivered = next(
                (
                    event
                    for event in self.pending_events
                    if event.event_id == event_id
                ),
                None,
            )
            before = len(self.pending_events)
            self.pending_events = [
                event for event in self.pending_events if event.event_id != event_id
            ]
            if len(self.pending_events) != before:
                if (
                    delivered is not None
                    and delivered.kind == "INITIAL"
                    and telegram_message_id is not None
                ):
                    signal = self.active.get(delivered.symbol)
                    if signal and signal.signal_id == delivered.signal_id:
                        signal.telegram_message_id = telegram_message_id
                    for event in self.pending_events:
                        if (
                            event.symbol == delivered.symbol
                            and event.signal_id == delivered.signal_id
                            and event.telegram_message_id is None
                        ):
                            event.telegram_message_id = telegram_message_id
                await self._save_unlocked()

    def pending_for(self, symbol: str) -> list[SignalEvent]:
        return [
            event for event in self.pending_events if event.symbol == symbol
        ]

    def _pending_for(self, symbol: str) -> list[SignalEvent]:
        return self.pending_for(symbol)

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
            provider=sig.provider,
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
            "schema_version": STATE_SCHEMA_VERSION,
            "provider": CURRENT_PROVIDER,
            "active": {k: asdict(v) for k, v in self.active.items()},
            "pending_events": [asdict(event) for event in self.pending_events],
            "last_signal_candles": self.last_signal_candles,
            "legacy_quarantine": self.legacy_quarantine,
        }
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)
