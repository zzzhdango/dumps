from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

import ccxt.async_support as ccxt
import pandas as pd

from config import Config

log = logging.getLogger(__name__)
T = TypeVar("T")
RETRYABLE = (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable, ccxt.DDoSProtection, ccxt.RateLimitExceeded)


class BingXPublicClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exchange = ccxt.bingx({"enableRateLimit": True, "timeout": cfg.request_timeout_ms,
                                    "options": {"defaultType": "swap"}})
        self.symbols: list[str] = []

    async def _retry(self, operation: Callable[[], Awaitable[T]], label: str) -> T:
        for attempt in range(self.cfg.max_retries):
            try:
                return await operation()
            except RETRYABLE as exc:
                if attempt + 1 >= self.cfg.max_retries:
                    raise
                delay = self.cfg.retry_base_seconds * (2 ** attempt)
                log.warning("%s: %s; повтор через %.1f c", label, exc, delay)
                await asyncio.sleep(delay)
        raise RuntimeError("Недостижимый код")

    async def initialize(self) -> None:
        markets = await self._retry(self.exchange.load_markets, "load_markets")
        available = {s for s, m in markets.items() if m.get("swap") and m.get("active", True) and m.get("quote") == "USDT"}
        if self.cfg.symbols:
            invalid = set(self.cfg.symbols) - available
            if invalid:
                log.warning("Пропущены недоступные BingX swap symbols: %s", sorted(invalid))
            self.symbols = [symbol for symbol in self.cfg.symbols if symbol in available]
        else:
            self.symbols = sorted(available)
        if not self.symbols:
            raise RuntimeError("На BingX не найдены активные USDT swap рынки")

    async def fetch_market(self, symbol: str) -> tuple[pd.DataFrame, float]:
        async def bars() -> Any:
            return await self.exchange.fetch_ohlcv(symbol, self.cfg.timeframe, limit=self.cfg.ohlcv_limit)
        async def ticker() -> Any:
            return await self.exchange.fetch_ticker(symbol)
        raw, tick = await asyncio.gather(self._retry(bars, f"OHLCV {symbol}"), self._retry(ticker, f"ticker {symbol}"))
        now_ms = int(time.time() * 1000)
        candle_ms = self.cfg.timeframe_minutes * 60 * 1000
        completed = [row for row in raw if int(row[0]) + candle_ms <= now_ms]
        frame = pd.DataFrame(completed, columns=["timestamp", "open", "high", "low", "close", "volume"])
        quote_volume = tick.get("quoteVolume")
        if quote_volume is None:
            quote_volume = (tick.get("info") or {}).get("quoteVolume", 0)
        return frame, float(quote_volume or 0)

    async def close(self) -> None:
        await self.exchange.close()
