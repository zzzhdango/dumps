from __future__ import annotations

import asyncio
import json
import os
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


class SignalStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.active: dict[str, ActiveSignal] = {}

    async def load(self) -> None:
        async with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.active = {k: ActiveSignal(**v) for k, v in data.get("active", {}).items()}
            except (OSError, ValueError, TypeError):
                self.active = {}

    def is_active(self, symbol: str) -> bool:
        return symbol in self.active

    async def add(self, evaluation: StrategyEvaluation) -> bool:
        if not evaluation.passed or evaluation.levels is None:
            return False
        async with self._lock:
            if evaluation.symbol in self.active:
                return False
            lv = evaluation.levels
            self.active[evaluation.symbol] = ActiveSignal(evaluation.symbol, evaluation.candle_timestamp,
                lv.entry, lv.tp1, lv.tp2, lv.tp3, lv.sl, evaluation.candle_timestamp)
            await self._save_unlocked()
            return True

    async def update_from_candles(self, symbol: str, candles: Any) -> str | None:
        async with self._lock:
            sig = self.active.get(symbol)
            if not sig:
                return None
            later = candles[candles["timestamp"] > sig.candle_timestamp]
            outcome = None
            for row in later.itertuples(index=False):
                # Консервативно: если на одной свече достигнуты SL и TP1, первым считается SL.
                if float(row.high) >= sig.sl:
                    outcome = "SL"
                    break
                if float(row.low) <= sig.tp1:
                    outcome = "TP1"
                    break
            if outcome:
                del self.active[symbol]
                await self._save_unlocked()
            return outcome

    async def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"active": {k: asdict(v) for k, v in self.active.items()}}
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)
