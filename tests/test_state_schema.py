import copy
import json

import pytest

from signals import SignalStore, StateValidationError
from state_preflight import PreflightError, validate_state_file


def valid_state() -> dict:
    return {
        "schema_version": 2,
        "provider": "binanceusdm",
        "active": {
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT",
                "candle_timestamp": 1_000,
                "entry": 100.0,
                "tp1": 94.5,
                "tp2": 90.0,
                "tp3": 85.0,
                "sl": 111.25,
                "created_at": 2_000,
                "expires_at": 3_000,
                "signal_id": "",
                "telegram_message_id": None,
                "tp1_hit": False,
                "tp2_hit": True,
                "tp3_hit": False,
                "provider": "binanceusdm",
            }
        },
        "pending_events": [
            {
                "event_id": "signal:TP1",
                "kind": "TP1",
                "symbol": "BTC/USDT:USDT",
                "signal_id": "",
                "entry": 100.0,
                "level_price": 94.5,
                "event_timestamp": 2_500,
                "telegram_message_id": None,
                "provider": "binanceusdm",
                "text": None,
            }
        ],
        "last_signal_candles": {"BTC/USDT:USDT": 1_000},
        "legacy_quarantine": {},
    }


def mutate(payload: dict, path: tuple, value, *, delete: bool = False) -> dict:
    result = copy.deepcopy(payload)
    target = result
    for key in path[:-1]:
        target = target[key]
    if delete:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return result


INVALID_CASES = [
    ("float_schema_version", lambda p: mutate(p, ("schema_version",), 2.0)),
    ("unknown_top", lambda p: mutate(p, ("extra",), 1)),
    ("missing_top", lambda p: mutate(p, ("legacy_quarantine",), None, delete=True)),
    (
        "unknown_active",
        lambda p: mutate(p, ("active", "BTC/USDT:USDT", "extra"), 1),
    ),
    (
        "truthy_string_bool",
        lambda p: mutate(p, ("active", "BTC/USDT:USDT", "tp1_hit"), "false"),
    ),
    (
        "bool_timestamp",
        lambda p: mutate(p, ("active", "BTC/USDT:USDT", "created_at"), True),
    ),
    (
        "bad_expires",
        lambda p: mutate(p, ("active", "BTC/USDT:USDT", "expires_at"), "3000"),
    ),
    (
        "bad_active_message_id",
        lambda p: mutate(
            p,
            ("active", "BTC/USDT:USDT", "telegram_message_id"),
            False,
        ),
    ),
    (
        "zero_active_message_id",
        lambda p: mutate(
            p,
            ("active", "BTC/USDT:USDT", "telegram_message_id"),
            0,
        ),
    ),
    (
        "wrong_active_provider",
        lambda p: mutate(
            p,
            ("active", "BTC/USDT:USDT", "provider"),
            "bingx",
        ),
    ),
    ("bad_event_id", lambda p: mutate(p, ("pending_events", 0, "event_id"), 7)),
    ("unknown_event", lambda p: mutate(p, ("pending_events", 0, "extra"), 1)),
    (
        "bool_event_timestamp",
        lambda p: mutate(p, ("pending_events", 0, "event_timestamp"), False),
    ),
    (
        "negative_event_timestamp",
        lambda p: mutate(p, ("pending_events", 0, "event_timestamp"), -1),
    ),
    (
        "bad_event_message_id",
        lambda p: mutate(p, ("pending_events", 0, "telegram_message_id"), "42"),
    ),
    ("bad_event_text", lambda p: mutate(p, ("pending_events", 0, "text"), 4)),
    (
        "bool_dedup_timestamp",
        lambda p: mutate(p, ("last_signal_candles", "BTC/USDT:USDT"), True),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [(valid_state(), True)]
    + [(factory(valid_state()), False) for _, factory in INVALID_CASES],
    ids=["valid"] + [name for name, _ in INVALID_CASES],
)
async def test_preflight_and_runtime_have_strict_schema_parity(
    tmp_path,
    payload,
    expected,
):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        validate_state_file(str(path))
        preflight_accepted = True
    except PreflightError:
        preflight_accepted = False

    store = SignalStore(str(path))
    try:
        await store.load()
        runtime_accepted = True
    except StateValidationError:
        runtime_accepted = False

    assert preflight_accepted is expected
    assert runtime_accepted is expected
    assert preflight_accepted == runtime_accepted
