from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from config import Config
from exchange import BinanceFuturesPublicClient, BinanceRestrictedLocation

PREFERRED_SYMBOL = "BTC/USDT:USDT"


def select_smoke_symbol(symbols: list[str]) -> str:
    if not symbols:
        raise RuntimeError("Каталог Binance Futures пуст")
    return PREFERRED_SYMBOL if PREFERRED_SYMBOL in symbols else symbols[0]


async def check() -> str:
    load_dotenv()
    cfg = Config.from_env()
    client = BinanceFuturesPublicClient(cfg)
    try:
        await client.initialize()
        symbol = select_smoke_symbol(client.symbols)
        candles, volume, current_price = await client.fetch_market(symbol)
        if candles.empty:
            raise RuntimeError("Не получено завершённых свечей")
        return (
            f"OK: Binance Futures public API, {len(client.symbols)} active "
            f"USDT-M perpetual markets, {symbol}, candles={len(candles)}, "
            f"quoteVolume={volume}, last={current_price:.8g}"
        )
    finally:
        await client.close()


async def main() -> int:
    try:
        print(await check())
        return 0
    except BinanceRestrictedLocation as exc:
        print(
            "ERROR: Binance Futures вернул HTTP 451: исходящий IP находится "
            f"в restricted location. {exc}",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"ERROR: Binance Futures public API smoke failed: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
