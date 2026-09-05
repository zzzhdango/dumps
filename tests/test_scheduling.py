from dataclasses import dataclass

from scheduling import cycle_delay, tracked_signal_symbols


@dataclass
class PendingEvent:
    symbol: str


def test_cycle_delay_keeps_start_to_start_interval():
    assert cycle_delay(300, 82.5, 10) == 217.5
    assert cycle_delay(60, 2.5, 1) == 57.5


def test_cycle_delay_uses_safe_minimum_after_slow_cycle():
    assert cycle_delay(300, 500, 10) == 10
    assert cycle_delay(60, 75, 1) == 1


def test_tracked_symbols_include_active_and_unsent_events_once():
    active = {
        "BTC/USDT:USDT": object(),
        "ETH/USDT:USDT": object(),
    }
    pending = [
        PendingEvent("ETH/USDT:USDT"),
        PendingEvent("XRP/USDT:USDT"),
    ]

    assert tracked_signal_symbols(active, pending) == [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "XRP/USDT:USDT",
    ]
