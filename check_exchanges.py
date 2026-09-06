from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from dotenv import load_dotenv

from config import Config
from exchange import (
    BinanceFuturesPublicClient,
    BinanceRestrictedLocation,
    is_binance_futures_market,
)


def is_scannable_market(market: dict[str, Any]) -> bool:
    """Expose the same strict Binance market predicate used by runtime."""
    return is_binance_futures_market(market)


def connectivity_summary(
    market_count: int,
    symbol: str,
    candle_count: int,
    quote_volume: float,
    current_price: float,
    elapsed: float,
) -> dict[str, Any]:
    return {
        "exchange": "binanceusdm",
        "market_type": "public linear USDT-settled perpetual futures",
        "ok": True,
        "active_markets": market_count,
        "test_symbol": symbol,
        "completed_ohlcv_rows": candle_count,
        "ticker_quote_volume": quote_volume,
        "ticker_last": current_price,
        "elapsed_seconds": round(elapsed, 2),
    }


async def probe() -> dict[str, Any]:
    load_dotenv()
    cfg = Config.from_env()
    client = BinanceFuturesPublicClient(cfg)
    started = time.monotonic()
    try:
        await client.initialize()
        symbol = (
            "BTC/USDT:USDT"
            if "BTC/USDT:USDT" in client.available_symbols
            else client.symbols[0]
        )
        candles, quote_volume, current_price = await client.fetch_market(symbol)
        return connectivity_summary(
            len(client.symbols),
            symbol,
            len(candles),
            quote_volume,
            current_price,
            time.monotonic() - started,
        )
    except BinanceRestrictedLocation as exc:
        return {
            "exchange": "binanceusdm",
            "ok": False,
            "restricted_location_451": True,
            "error": str(exc),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    except Exception as exc:
        return {
            "exchange": "binanceusdm",
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    finally:
        await client.close()


async def main() -> int:
    result = await probe()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("restricted_location_451"):
        print(
            "\nBinance Futures недоступен: HTTP 451 restricted location "
            "для исходящего IP.",
            file=sys.stderr,
        )
        return 2
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
