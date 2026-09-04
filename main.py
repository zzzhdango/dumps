from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot
from dotenv import load_dotenv

from bot import build_dispatcher, format_signal
from config import Config
from exchange import BingXPublicClient
from signals import SignalStore
from strategy import evaluate_strategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


async def scan_forever(cfg: Config, client: BingXPublicClient, store: SignalStore, bot: Bot, runtime: dict) -> None:
    while True:
        try:
            for symbol in client.symbols:
                try:
                    candles, quote_volume = await client.fetch_market(symbol)
                    await store.update_from_candles(symbol, candles)
                    required_bars = 24 * 60 // cfg.timeframe_minutes + 24
                    if store.is_active(symbol) or len(candles) < required_bars:
                        continue
                    evaluation = evaluate_strategy(symbol, candles, quote_volume, cfg)
                    if evaluation.passed and await store.add(evaluation):
                        await bot.send_message(cfg.telegram_chat_id, format_signal(evaluation))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Ошибка обработки %s; сканирование продолжено", symbol)
            runtime["last_scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка цикла сканера")
        await asyncio.sleep(cfg.scanner_interval)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def run() -> None:
    load_dotenv()
    cfg = Config.from_env()
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        raise ValueError("BOT_TOKEN и TELEGRAM_CHAT_ID обязательны")
    store = SignalStore(cfg.state_file)
    await store.load()
    client = BingXPublicClient(cfg)
    bot = Bot(cfg.telegram_bot_token)
    runtime: dict = {}
    runner: web.AppRunner | None = None
    scanner: asyncio.Task | None = None
    try:
        await client.initialize()
        runtime["market_count"] = len(client.symbols)
        log.info("Загружено активных BingX USDT swap рынков: %d", len(client.symbols))
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, cfg.health_host, cfg.health_port).start()
        scanner = asyncio.create_task(scan_forever(cfg, client, store, bot, runtime))
        dispatcher = build_dispatcher(cfg, store, runtime)
        await dispatcher.start_polling(bot)
    finally:
        if scanner:
            scanner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scanner
        if runner:
            await runner.cleanup()
        await client.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
