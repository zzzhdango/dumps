from __future__ import annotations

import asyncio
from collections import deque
import logging
import re
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


def bingx_cooldown_until_ms(exc: BaseException) -> int | None:
    text = str(exc)
    if '"code":109429' not in text and "code:109429" not in text:
        return None
    match = re.search(r"retry after time:\s*(\d{13})", text)
    if match:
        return int(match.group(1))
    return int(time.time() * 1000) + 900_000


def is_crypto_swap_market(market: dict[str, Any]) -> bool:
    if not (
        market.get("swap")
        and market.get("active", True)
        and market.get("quote") == "USDT"
    ):
        return False
    status = (market.get("info") or {}).get("status")
    # BingX status=25 обозначает TradFi perpetuals: акции, FX, индексы,
    # металлы и сырьё. status=1 используется криптовалютными контрактами.
    # Отсутствующий status допускается для совместимости с ccxt-моками.
    return status is None or str(status) == "1"


class BingXPublicClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exchange = ccxt.bingx({"enableRateLimit": True, "timeout": cfg.request_timeout_ms,
                                    "options": {"defaultType": "swap"}})
        self.symbols: list[str] = []
        self.available_symbols: set[str] = set()
        self.unavailable_symbols: set[str] = set()
        self.paused_retry_at: dict[str, float] = {}
        self.paused_probe_attempts: deque[float] = deque()
        self.authorized_paused_probes: set[str] = set()
        self.excluded_tradfi_count = 0
        self.api_cooldown_until_ms = 0

    async def _wait_for_api_cooldown(self, label: str) -> None:
        delay = (self.api_cooldown_until_ms - int(time.time() * 1000)) / 1000
        if delay > 0:
            log.warning(
                "%s: BingX API cooldown, ожидание %.1f сек",
                label,
                delay,
            )
            await asyncio.sleep(delay + 1)

    async def _retry(self, operation: Callable[[], Awaitable[T]], label: str) -> T:
        retryable_attempt = 0
        while True:
            await self._wait_for_api_cooldown(label)
            try:
                return await operation()
            except RETRYABLE as exc:
                retryable_attempt += 1
                if retryable_attempt >= self.cfg.max_retries:
                    raise
                delay = self.cfg.retry_base_seconds * (
                    2 ** (retryable_attempt - 1)
                )
                log.warning("%s: %s; повтор через %.1f c", label, exc, delay)
                await asyncio.sleep(delay)
            except ccxt.ExchangeError as exc:
                cooldown_until = bingx_cooldown_until_ms(exc)
                if cooldown_until is None:
                    raise
                self.api_cooldown_until_ms = max(
                    self.api_cooldown_until_ms,
                    cooldown_until,
                )
                # 109429 не расходует обычные retry-попытки: BingX сам
                # сообщает безопасное время продолжения запросов.
                await self._wait_for_api_cooldown(label)

    async def _reload_symbols(self, reload: bool) -> tuple[set[str], set[str]]:
        async def load() -> Any:
            return await self.exchange.load_markets(reload)

        markets = await self._retry(load, "reload_markets" if reload else "load_markets")
        all_active_usdt_swaps = {
            symbol
            for symbol, market in markets.items()
            if market.get("swap")
            and market.get("active", True)
            and market.get("quote") == "USDT"
        }
        available = {
            symbol
            for symbol, market in markets.items()
            if is_crypto_swap_market(market)
        }
        self.excluded_tradfi_count = len(all_active_usdt_swaps - available)
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
        self.paused_retry_at = {
            symbol: retry_at
            for symbol, retry_at in self.paused_retry_at.items()
            if symbol in current
        }
        self.authorized_paused_probes.intersection_update(current)
        return current - previous, previous - current

    async def initialize(self) -> None:
        await self._reload_symbols(reload=False)

    async def refresh_markets(self) -> tuple[set[str], set[str]]:
        """Reload the public BingX catalog and return added/removed symbols."""
        return await self._reload_symbols(reload=True)

    def prepare_scan_symbols(
        self,
    ) -> tuple[list[str], set[str], set[str]]:
        """Return available symbols plus a bounded batch of due paused probes."""
        previous = set(self.unavailable_symbols)
        now = time.monotonic()
        window_start = now - self.cfg.paused_recheck_interval
        while (
            self.paused_probe_attempts
            and self.paused_probe_attempts[0] <= window_start
        ):
            self.paused_probe_attempts.popleft()
        available_budget = max(0, 5 - len(self.paused_probe_attempts))
        probe_count = min(
            self.cfg.paused_recheck_batch,
            available_budget,
        )
        retrying = set(
            sorted(
                symbol
                for symbol in self.unavailable_symbols
                if self.paused_retry_at.get(symbol, 0) <= now
                and symbol not in self.authorized_paused_probes
            )[:probe_count]
        )
        self.paused_probe_attempts.extend(now for _ in retrying)
        self.authorized_paused_probes.update(retrying)
        symbols = [
            symbol
            for symbol in self.symbols
            if symbol not in self.unavailable_symbols or symbol in retrying
        ]
        return symbols, previous, retrying

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
        probing_paused = symbol in self.unavailable_symbols
        if probing_paused:
            if symbol not in self.authorized_paused_probes:
                raise MarketUnavailable(
                    f"{symbol}: paused-рынок ожидает безопасной повторной проверки"
                )
            self.authorized_paused_probes.discard(symbol)

        async def bars() -> Any:
            return await self.exchange.fetch_ohlcv(symbol, self.cfg.timeframe, limit=self.cfg.ohlcv_limit)
        async def ticker() -> Any:
            return await self.exchange.fetch_ticker(symbol)
        # Candles идут первыми: если BingX объявил рынок paused, не запускаем
        # лишний ticker request и не оставляем retry-задачу в фоне.
        try:
            raw = await self._retry(bars, f"OHLCV {symbol}")
        except ccxt.ExchangeError as exc:
            if is_paused_market_error(exc):
                self.unavailable_symbols.add(symbol)
                self.paused_retry_at[symbol] = (
                    time.monotonic() + self.cfg.paused_recheck_interval
                )
                raise MarketUnavailable(f"{symbol}: публичные свечи BingX временно приостановлены") from exc
            raise
        except Exception:
            if probing_paused:
                self.paused_retry_at[symbol] = (
                    time.monotonic() + self.cfg.paused_recheck_interval
                )
            raise

        # Успешные свечи подтверждают восстановление paused-рынка, даже если
        # последующий ticker-запрос завершится временной ошибкой.
        self.unavailable_symbols.discard(symbol)
        self.paused_retry_at.pop(symbol, None)
        tick = await self._retry(ticker, f"ticker {symbol}")
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
