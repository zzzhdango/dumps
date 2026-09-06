from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import random
import re
import time
from typing import Any, Awaitable, Callable, TypeVar

import ccxt.async_support as ccxt
import pandas as pd

from config import Config

log = logging.getLogger(__name__)
T = TypeVar("T")
RETRYABLE = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
    ccxt.DDoSProtection,
    ccxt.RateLimitExceeded,
)


class MarketUnavailable(RuntimeError):
    """The requested contract disappeared or cannot currently be traded."""


class BinanceRestrictedLocation(RuntimeError):
    """Binance public Futures API is unavailable from the current location."""


class RequestLocalBinanceUSDM(ccxt.binanceusdm):
    """Preserve response metadata on the exact exception raised by CCXT."""

    def handle_errors(
        self,
        code: int,
        reason: str,
        url: str,
        method: str,
        headers: dict,
        body: str,
        response: Any,
        request_headers: Any,
        request_body: Any,
    ) -> Any:
        try:
            return super().handle_errors(
                code,
                reason,
                url,
                method,
                headers,
                body,
                response,
                request_headers,
                request_body,
            )
        except Exception as exc:
            # CCXT 4.5.x stores these headers only on the shared exchange
            # object. Attach a request-local copy before the exception leaves
            # this synchronous response boundary so concurrent responses can
            # never overwrite the metadata used by retry logic.
            exc.response_headers = dict(headers or {})
            exc.status_code = code
            raise


def is_binance_futures_market(market: dict[str, Any]) -> bool:
    """Accept only active linear USDT-settled perpetual swaps."""
    if not (
        market.get("swap") is True
        and market.get("linear") is True
        and market.get("quote") == "USDT"
        and market.get("settle") == "USDT"
        and market.get("active") is True
    ):
        return False
    info = market.get("info") or {}
    status = info.get("status")
    contract_type = info.get("contractType")
    if status is not None and str(status).upper() != "TRADING":
        return False
    if (
        contract_type is not None
        and str(contract_type).upper() != "PERPETUAL"
    ):
        return False
    return True


def is_restricted_location_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    return (
        status == 451
        or bool(re.search(r"\b451\b", text))
        or "restricted location" in text
        or "service unavailable from a restricted location" in text
        or "not available in your region" in text
    )


def is_market_unavailable_error(exc: BaseException) -> bool:
    if isinstance(exc, ccxt.BadSymbol):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "bad symbol",
            "invalid symbol",
            "does not have market symbol",
            "market is closed",
            "market closed",
            "delist",
            '"code":-1121',
            "code=-1121",
        )
    )


def _exception_headers(exc: BaseException) -> dict[str, Any]:
    for name in ("response_headers", "headers"):
        value = getattr(exc, name, None)
        if isinstance(value, dict):
            return value
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    return dict(headers) if headers is not None else {}


def retry_after_seconds(
    exc: BaseException,
    fallback: float,
    now: float | None = None,
) -> float:
    """Read Retry-After seconds/date when present, otherwise use fallback."""
    value: Any = None
    headers = _exception_headers(exc)
    for key, header_value in headers.items():
        if str(key).lower() == "retry-after":
            value = header_value
            break
    if value is None:
        match = re.search(
            r"retry[- ]after(?:\s*[:=]\s*|\D+)(\d+(?:\.\d+)?)",
            str(exc),
            re.IGNORECASE,
        )
        if match:
            value = match.group(1)
    if value is None:
        return fallback
    try:
        delay = float(value)
        return delay if delay > 0 else fallback
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(value))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            current = (
                datetime.fromtimestamp(now, timezone.utc)
                if now is not None
                else datetime.now(timezone.utc)
            )
            delay = (target - current).total_seconds()
            return delay if delay > 0 else fallback
        except (TypeError, ValueError, OverflowError):
            return fallback


def is_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        return True
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status in {418, 429} or bool(re.search(r"\b(?:418|429)\b", text))


def rate_limit_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    try:
        numeric = int(status)
        if numeric in {418, 429}:
            return numeric
    except (TypeError, ValueError):
        pass
    match = re.search(r"\b(418|429)\b", str(exc))
    return int(match.group(1)) if match else None


class BinanceFuturesPublicClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exchange = RequestLocalBinanceUSDM(
            {
                "enableRateLimit": True,
                "timeout": cfg.request_timeout_ms,
            }
        )
        self.symbols: list[str] = []
        self.available_symbols: set[str] = set()
        self.blocked_until = 0.0
        self._cooldown_lock = asyncio.Lock()
        self._market_lock = asyncio.Lock()

    def _ensure_runtime_primitives(self) -> None:
        # Keeps lightweight object.__new__ test doubles backwards-compatible.
        if not hasattr(self, "blocked_until"):
            self.blocked_until = 0.0
        if not hasattr(self, "_cooldown_lock"):
            self._cooldown_lock = asyncio.Lock()
        if not hasattr(self, "_market_lock"):
            self._market_lock = asyncio.Lock()

    async def _wait_for_cooldown(self) -> None:
        self._ensure_runtime_primitives()
        while True:
            async with self._cooldown_lock:
                delay = self.blocked_until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    async def _set_rate_limit_cooldown(
        self,
        exc: BaseException,
        exponential: float,
    ) -> float:
        self._ensure_runtime_primitives()
        status = rate_limit_status(exc)
        fallback = max(exponential, 60.0 if status == 418 else 1.0)
        server_delay = retry_after_seconds(exc, fallback)
        base_delay = max(exponential, server_delay)
        jitter = random.uniform(0.0, min(1.0, base_delay * 0.1))
        delay = base_delay + jitter
        async with self._cooldown_lock:
            self.blocked_until = max(
                self.blocked_until,
                time.monotonic() + delay,
            )
        return delay

    async def _retry(
        self,
        operation: Callable[[], Awaitable[T]],
        label: str,
    ) -> T:
        self._ensure_runtime_primitives()
        for attempt in range(1, self.cfg.max_retries + 1):
            await self._wait_for_cooldown()
            try:
                return await operation()
            except RETRYABLE as exc:
                if is_restricted_location_error(exc):
                    raise BinanceRestrictedLocation(
                        "Binance Futures недоступен из текущей локации (HTTP 451)"
                    ) from exc
                exponential = self.cfg.retry_base_seconds * (2 ** (attempt - 1))
                if is_rate_limit_error(exc):
                    delay = await self._set_rate_limit_cooldown(
                        exc,
                        exponential,
                    )
                else:
                    delay = exponential
                if attempt >= self.cfg.max_retries:
                    raise
                log.warning("%s: %s; повтор через %.1f с", label, exc, delay)
                if not is_rate_limit_error(exc):
                    await asyncio.sleep(delay)
            except ccxt.ExchangeError as exc:
                if is_restricted_location_error(exc):
                    raise BinanceRestrictedLocation(
                        "Binance Futures недоступен из текущей локации (HTTP 451)"
                    ) from exc
                if is_market_unavailable_error(exc):
                    raise MarketUnavailable(
                        f"{label}: рынок Binance Futures недоступен"
                    ) from exc
                if is_rate_limit_error(exc):
                    exponential = self.cfg.retry_base_seconds * (
                        2 ** (attempt - 1)
                    )
                    delay = await self._set_rate_limit_cooldown(
                        exc,
                        exponential,
                    )
                    if attempt >= self.cfg.max_retries:
                        raise
                    log.warning(
                        "%s: %s; повтор через %.1f с",
                        label,
                        exc,
                        delay,
                    )
                    continue
                raise
        raise AssertionError("unreachable")

    async def _reload_symbols(
        self,
        reload: bool,
    ) -> tuple[set[str], set[str]]:
        self._ensure_runtime_primitives()
        async with self._market_lock:
            async def load() -> Any:
                return await self.exchange.load_markets(reload)

            markets = await self._retry(
                load,
                "reload_markets" if reload else "load_markets",
            )
            available = {
                symbol
                for symbol, market in markets.items()
                if is_binance_futures_market(market)
            }
            if self.cfg.symbols:
                invalid = set(self.cfg.symbols) - available
                if invalid:
                    log.warning(
                        "Пропущены недоступные Binance Futures symbols: %s",
                        sorted(invalid),
                    )
                updated = [
                    symbol
                    for symbol in self.cfg.symbols
                    if symbol in available
                ]
            else:
                updated = sorted(available)
            if not updated and not reload:
                raise RuntimeError(
                    "На Binance Futures не найдены активные USDT-M "
                    "perpetual рынки"
                )
            previous = set(self.symbols)
            current = set(updated)
            # Commit one complete snapshot without an await between fields.
            self.available_symbols = available
            self.symbols = updated
            return current - previous, previous - current

    async def initialize(self) -> None:
        await self._reload_symbols(reload=False)

    async def refresh_markets(self) -> tuple[set[str], set[str]]:
        """Reload Binance Futures markets after a complete scan cycle."""
        return await self._reload_symbols(reload=True)

    def resolve_symbol(self, raw: str) -> str | None:
        """Resolve BTC, BTCUSDT or BTC/USDT:USDT to an active contract."""
        value = raw.strip().upper().replace(" ", "")
        if not value:
            return None
        if value.endswith("/USDT:USDT"):
            candidate = value
        elif value.endswith("USDT"):
            candidate = f"{value[:-4].rstrip('/:-')}/USDT:USDT"
        else:
            candidate = f"{value}/USDT:USDT"
        return candidate if candidate in self.available_symbols else None

    async def fetch_market(
        self,
        symbol: str,
    ) -> tuple[pd.DataFrame, float, float]:
        if symbol not in self.available_symbols:
            raise MarketUnavailable(
                f"{symbol}: рынок отсутствует в каталоге Binance Futures"
            )

        async def bars() -> Any:
            return await self.exchange.fetch_ohlcv(
                symbol,
                self.cfg.timeframe,
                limit=self.cfg.ohlcv_limit,
            )

        async def ticker() -> Any:
            return await self.exchange.fetch_ticker(symbol)

        try:
            raw = await self._retry(bars, f"OHLCV {symbol}")
            tick = await self._retry(ticker, f"ticker {symbol}")
        except ccxt.BadSymbol as exc:
            raise MarketUnavailable(
                f"{symbol}: рынок Binance Futures недоступен"
            ) from exc

        now_ms = int(time.time() * 1000)
        candle_ms = self.cfg.timeframe_minutes * 60 * 1000
        completed = [
            row for row in raw if int(row[0]) + candle_ms <= now_ms
        ]
        frame = pd.DataFrame(
            completed,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ],
        )
        quote_volume = tick.get("quoteVolume")
        if quote_volume is None:
            info = tick.get("info") or {}
            quote_volume = info.get("quoteVolume", info.get("q", 0))
        current_price = tick.get("last") or tick.get("close")
        if current_price is None:
            current_price = completed[-1][4] if completed else 0
        return frame, float(quote_volume or 0), float(current_price or 0)

    async def close(self) -> None:
        await self.exchange.close()
