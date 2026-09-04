from __future__ import annotations

import asyncio
from dotenv import load_dotenv

from config import Config
from exchange import BingXPublicClient


async def check() -> None:
    load_dotenv()
    cfg = Config.from_env()
    client = BingXPublicClient(cfg)
    try:
        await client.initialize()
        symbol = client.symbols[0]
        candles, volume = await client.fetch_market(symbol)
        required_bars = 24 * 60 // cfg.timeframe_minutes + 24
        if len(candles) < required_bars:
            raise RuntimeError(f"Получено только {len(candles)} завершённых свечей")
        print(f"OK: BingX public API, {len(client.symbols)} swap markets, {symbol}, candles={len(candles)}, quoteVolume={volume}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(check())
