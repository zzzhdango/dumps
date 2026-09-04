import pandas as pd
import pytest

from signals import SignalStore
from strategy import SignalLevels, StrategyEvaluation


@pytest.mark.asyncio
async def test_dedup_persistence_and_conservative_close(tmp_path):
    path = tmp_path / "state.json"
    store = SignalStore(str(path))
    levels = SignalLevels(100, 94.5, 90, 85, 111.25, 3, None, None, None)
    ev = StrategyEvaluation("X/USDT:USDT", True, 1000, {}, {}, levels, ("ok",))
    assert await store.add(ev)
    assert not await store.add(ev)
    restored = SignalStore(str(path))
    await restored.load()
    assert restored.is_active(ev.symbol)
    both = pd.DataFrame([{"timestamp": 2000, "open": 100, "high": 112, "low": 94, "close": 100, "volume": 1}])
    assert await restored.update_from_candles(ev.symbol, both) == "SL"
    assert not restored.is_active(ev.symbol)
