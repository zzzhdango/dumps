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


class MarketUnavailable(RuntimeError):
    """BingX advertises the market, but public candles are currently paused."""


def is_paused_market_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return '"code":109415' in text or ("is pause currently" in text and "bingx" in text)


class BingXPublicClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exchange = ccxt.bingx({"enableRateLimit": True, "timeout": cfg.request_timeout_ms,
                                    "options": {"defaultType": "swap"}})
        self.symbols: list[str] = []
        self.available_symbols: set[str] = set()
        self.unavailable_symbols: set[str] = set()

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

    async def _reload_symbols(self, reload: bool) -> tuple[set[str], set[str]]:
        async def load() -> Any:
            return await self.exchange.load_markets(reload)

        markets = await self._retry(load, "reload_markets" if reload else "load_markets")
        available = {s for s, m in markets.items() if m.get("swap") and m.get("active", True) and m.get("quote") == "USDT"}
        self.available_symbols = available
        if self.cfg.symbols:
            invalid = set(self.cfg.symbols) - available
            if invalid:
                log.warning("Пропущены недоступные BingX swap symbols: %s", sorted(invalid))
            updated = [symbol for symbol in self.cfg.symbols if symbol in available]
        else:
            updated = sorted(available)
        if not updated:
            raise RuntimeError("На BingX не найдены активные USDT swap рынки")
        previous = set(self.symbols)
        current = set(updated)
        self.symbols = updated
        self.unavailable_symbols.intersection_update(current)
        return current - previous, previous - current

    async def initialize(self) -> None:
        await self._reload_symbols(reload=False)

    async def refresh_markets(self) -> tuple[set[str], set[str]]:
        """Reload the public BingX catalog and return added/removed symbols."""
        return await self._reload_symbols(reload=True)

    def reset_paused_for_recheck(self) -> set[str]:
        """Return the previous paused set and allow every symbol to be retried."""
        previous = set(self.unavailable_symbols)
        self.unavailable_symbols.clear()
        return previous

    def resolve_symbol(self, raw: str) -> str | None:
        """Resolve BTC, BTCUSDT or a unified symbol to an active BingX USDT swap."""
        value = raw.strip().upper().replace(" ", "")
        if not value:
            return None
        if value.endswith("/USDT:USDT"):
            candidate = value
        elif value.endswith("USDT"):
            base = value[:-4].rstrip("/:-")
            candidate = f"{base}/USDT:USDT"
        else:
            candidate = f"{value}/USDT:USDT"
        return candidate if candidate in self.available_symbols else None

    async def fetch_market(self, symbol: str) -> tuple[pd.DataFrame, float, float]:
        async def bars() -> Any:
            return await self.exchange.fetch_ohlcv(symbol, self.cfg.timeframe, limit=self.cfg.ohlcv_limit)
        async def ticker() -> Any:
            return await self.exchange.fetch_ticker(symbol)
        try:
            # Candles идут первыми: если BingX объявил рынок paused, не запускаем
            # лишний ticker request и не оставляем retry-задачу в фоне.
            raw = await self._retry(bars, f"OHLCV {symbol}")
            tick = await self._retry(ticker, f"ticker {symbol}")
        except ccxt.ExchangeError as exc:
            if is_paused_market_error(exc):
                self.unavailable_symbols.add(symbol)
                raise MarketUnavailable(f"{symbol}: публичные свечи BingX временно приостановлены") from exc
            raise
        now_ms = int(time.time() * 1000)
        candle_ms = self.cfg.timeframe_minutes * 60 * 1000
        completed = [row for row in raw if int(row[0]) + candle_ms <= now_ms]
        frame = pd.DataFrame(completed, columns=["timestamp", "open", "high", "low", "close", "volume"])
        quote_volume = tick.get("quoteVolume")
        if quote_volume is None:
            quote_volume = (tick.get("info") or {}).get("quoteVolume", 0)
        current_price = tick.get("last") or tick.get("close")
        if current_price is None:
            current_price = completed[-1][4] if completed else 0
        return frame, float(quote_volume or 0), float(current_price or 0)

    async def close(self) -> None:
        await self.exchange.close()
