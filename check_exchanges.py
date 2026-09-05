from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import ccxt.async_support as ccxt


EXCHANGES = ("bingx", "binanceusdm", "weex")
TEST_SYMBOL = "BTC/USDT:USDT"


def is_scannable_market(exchange_id: str, market: dict[str, Any]) -> bool:
    """Return True only for active linear USDT perpetual crypto markets."""
    if not (
        market.get("swap")
        and market.get("linear")
        and market.get("quote") == "USDT"
        and market.get("active") is not False
    ):
        return False

    if exchange_id == "weex":
        info = market.get("info") or {}
        return (
            info.get("contractType") == "PERPETUAL"
            and info.get("underlyingType") == "COIN"
        )

    return True


async def probe(exchange_id: str) -> dict[str, Any]:
    started = time.monotonic()
    exchange_class = getattr(ccxt, exchange_id, None)
    result: dict[str, Any] = {
        "exchange": exchange_id,
        "ccxt_supported": exchange_class is not None,
        "ccxt_version": ccxt.__version__,
    }
    if exchange_class is None:
        result["ok"] = False
        result["error"] = (
            f"CCXT {ccxt.__version__} does not contain exchange id {exchange_id!r}"
        )
        return result

    exchange = exchange_class(
        {
            "enableRateLimit": True,
            "timeout": 30_000,
            "options": {"defaultType": "swap"},
        }
    )
    try:
        markets = await exchange.load_markets()
        scannable = [
            market
            for market in markets.values()
            if is_scannable_market(exchange_id, market)
        ]
        result["scannable_markets"] = len(scannable)

        symbol = TEST_SYMBOL
        if symbol not in markets and scannable:
            symbol = scannable[0]["symbol"]
        result["test_symbol"] = symbol

        candles = await exchange.fetch_ohlcv(symbol, "15m", limit=3)
        ticker = await exchange.fetch_ticker(symbol)
        result.update(
            {
                "ok": True,
                "ohlcv_rows": len(candles),
                "last_closed_or_current_close": candles[-1][4] if candles else None,
                "ticker_last": ticker.get("last"),
            }
        )
    except Exception as exc:
        message = str(exc)
        result.update(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": message,
                "restricted_location_451": " 451 " in f" {message} "
                or "restricted location" in message.lower(),
            }
        )
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        await exchange.close()
    return result


async def main() -> int:
    results = await asyncio.gather(*(probe(exchange_id) for exchange_id in EXCHANGES))
    print(json.dumps(results, ensure_ascii=False, indent=2))

    working = [item["exchange"] for item in results if item.get("ok")]
    blocked = [
        item["exchange"]
        for item in results
        if item.get("restricted_location_451")
    ]
    print(f"\nРаботают из этого контейнера: {', '.join(working) or 'нет'}")
    if blocked:
        print(f"HTTP 451 по геолокации исходящего IP: {', '.join(blocked)}")
    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
