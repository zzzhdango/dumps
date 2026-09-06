"""Single source of truth for persistent state schema validation."""

from __future__ import annotations

import math
from typing import Any

STATE_SCHEMA_VERSION = 2
CURRENT_PROVIDER = "binanceusdm"

TOP_LEVEL_KEYS = {
    "schema_version",
    "provider",
    "active",
    "pending_events",
    "last_signal_candles",
    "legacy_quarantine",
}
ACTIVE_KEYS = {
    "symbol",
    "candle_timestamp",
    "entry",
    "tp1",
    "tp2",
    "tp3",
    "sl",
    "created_at",
    "expires_at",
    "signal_id",
    "telegram_message_id",
    "tp1_hit",
    "tp2_hit",
    "tp3_hit",
    "provider",
}
EVENT_KEYS = {
    "event_id",
    "kind",
    "symbol",
    "signal_id",
    "entry",
    "level_price",
    "event_timestamp",
    "telegram_message_id",
    "provider",
    "text",
}
EVENT_KINDS = {"INITIAL", "TP1", "TP2", "TP3", "SL"}


class StateSchemaError(ValueError):
    pass


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if unknown:
            details.append(f"unknown {sorted(unknown)}")
        raise StateSchemaError(f"{label}: {', '.join(details)}")


def _integer(value: Any, label: str, *, nonnegative: bool = False) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StateSchemaError(f"{label} must be an integer, not bool")
    if nonnegative and value < 0:
        raise StateSchemaError(f"{label} must be nonnegative")


def _number(value: Any, label: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise StateSchemaError(f"{label} must be a positive finite number")


def _string(value: Any, label: str, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value):
        suffix = "a string" if allow_empty else "a non-empty string"
        raise StateSchemaError(f"{label} must be {suffix}")


def _nullable_integer(value: Any, label: str) -> None:
    if value is not None:
        _integer(value, label)
        if value <= 0:
            raise StateSchemaError(f"{label} must be positive when present")


def validate_active_record(
    key: str,
    item: Any,
    *,
    exact: bool = True,
    require_current_provider: bool = True,
) -> None:
    _string(key, "active key")
    if not isinstance(item, dict):
        raise StateSchemaError(f"active[{key!r}] must be an object")
    if exact:
        _exact_keys(item, ACTIVE_KEYS, f"active[{key!r}]")
    _string(item.get("symbol"), f"active[{key!r}].symbol")
    if item["symbol"] != key:
        raise StateSchemaError(f"active[{key!r}] symbol mismatch")
    for field in ("candle_timestamp", "created_at", "expires_at"):
        _integer(
            item.get(field),
            f"active[{key!r}].{field}",
            nonnegative=True,
        )
    for field in ("entry", "tp1", "tp2", "tp3", "sl"):
        _number(item.get(field), f"active[{key!r}].{field}")
    _string(item.get("signal_id"), f"active[{key!r}].signal_id", allow_empty=True)
    _nullable_integer(
        item.get("telegram_message_id"),
        f"active[{key!r}].telegram_message_id",
    )
    for field in ("tp1_hit", "tp2_hit", "tp3_hit"):
        if type(item.get(field)) is not bool:
            raise StateSchemaError(f"active[{key!r}].{field} must be bool")
    _string(item.get("provider"), f"active[{key!r}].provider")
    if require_current_provider and item["provider"] != CURRENT_PROVIDER:
        raise StateSchemaError(f"active[{key!r}].provider is not current")


def validate_event_record(item: Any, index: int, *, exact: bool = True) -> None:
    label = f"pending_events[{index}]"
    if not isinstance(item, dict):
        raise StateSchemaError(f"{label} must be an object")
    if exact:
        _exact_keys(item, EVENT_KEYS, label)
    _string(item.get("event_id"), f"{label}.event_id")
    _string(item.get("kind"), f"{label}.kind")
    if item["kind"] not in EVENT_KINDS:
        raise StateSchemaError(f"{label}.kind is invalid")
    _string(item.get("symbol"), f"{label}.symbol")
    _string(item.get("signal_id"), f"{label}.signal_id", allow_empty=True)
    _number(item.get("entry"), f"{label}.entry")
    _number(item.get("level_price"), f"{label}.level_price")
    _integer(
        item.get("event_timestamp"),
        f"{label}.event_timestamp",
        nonnegative=True,
    )
    _nullable_integer(item.get("telegram_message_id"), f"{label}.telegram_message_id")
    _string(item.get("provider"), f"{label}.provider")
    text = item.get("text")
    if text is not None and not isinstance(text, str):
        raise StateSchemaError(f"{label}.text must be string or null")
    if item["kind"] == "INITIAL" and not text:
        raise StateSchemaError(f"{label}.text is required for INITIAL")


def validate_current_state_data(data: Any) -> None:
    if not isinstance(data, dict):
        raise StateSchemaError("state root must be an object")
    _exact_keys(data, TOP_LEVEL_KEYS, "state")
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != STATE_SCHEMA_VERSION
    ):
        raise StateSchemaError("unsupported schema_version")
    if data["provider"] != CURRENT_PROVIDER:
        raise StateSchemaError("unexpected provider")
    active = data["active"]
    pending = data["pending_events"]
    dedup = data["last_signal_candles"]
    quarantine = data["legacy_quarantine"]
    if not isinstance(active, dict):
        raise StateSchemaError("active must be dict")
    if not isinstance(pending, list):
        raise StateSchemaError("pending_events must be list")
    if not isinstance(dedup, dict):
        raise StateSchemaError("last_signal_candles must be dict")
    if not isinstance(quarantine, dict):
        raise StateSchemaError("legacy_quarantine must be dict")
    for key, item in active.items():
        validate_active_record(key, item)
    for index, item in enumerate(pending):
        validate_event_record(item, index)
    for symbol, timestamp in dedup.items():
        _string(symbol, "last_signal_candles key")
        _integer(
            timestamp,
            f"last_signal_candles[{symbol!r}]",
            nonnegative=True,
        )
