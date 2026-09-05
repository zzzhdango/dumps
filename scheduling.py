from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol


class PendingSignalEvent(Protocol):
    symbol: str


def cycle_delay(
    interval_seconds: float,
    elapsed_seconds: float,
    minimum_delay: float,
) -> float:
    return max(minimum_delay, interval_seconds - elapsed_seconds)


def tracked_signal_symbols(
    active: Mapping[str, object],
    pending_events: Iterable[PendingSignalEvent],
) -> list[str]:
    symbols = set(active)
    symbols.update(event.symbol for event in pending_events)
    return sorted(symbols)
