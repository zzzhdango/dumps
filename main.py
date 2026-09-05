from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, ReplyParameters
from dotenv import load_dotenv

from bot import build_dispatcher
from config import Config
from exchange import BingXPublicClient, MarketUnavailable
from signal_report import build_signal_id, format_signal, format_signal_event
from signals import SignalEvent, SignalStore
from scheduling import cycle_delay, tracked_signal_symbols
from strategy import evaluate_strategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


async def send_signal_event(bot: Bot, chat_id: str, event: SignalEvent) -> None:
    text = format_signal_event(event)
    if event.telegram_message_id is None:
        await bot.send_message(chat_id, text)
        return
    try:
        await bot.send_message(
            chat_id,
            text,
            reply_parameters=ReplyParameters(message_id=event.telegram_message_id),
        )
    except TelegramBadRequest:
        log.warning(
            "Не удалось ответить на исходное сообщение %s; событие %s отправляется отдельно",
            event.telegram_message_id,
            event.event_id,
        )
        await bot.send_message(chat_id, text)


async def deliver_signal_events(
    bot: Bot,
    cfg: Config,
    store: SignalStore,
    events: list[SignalEvent],
) -> None:
    for event in events:
        await send_signal_event(bot, cfg.telegram_chat_id, event)
        await store.acknowledge_event(event.event_id)


async def scan_forever(
    cfg: Config,
    client: BingXPublicClient,
    store: SignalStore,
    bot: Bot,
    runtime: dict,
    symbol_locks: dict[str, asyncio.Lock],
) -> None:
    semaphore = asyncio.Semaphore(cfg.scan_concurrency)

    while True:
        cycle_started = time.monotonic()
        previously_paused = client.reset_paused_for_recheck()
        # Каждый новый цикл повторно проверяет paused-рынки. Если свечи снова
        # доступны, символ не попадёт в новый список unavailable_symbols.
        runtime["unavailable_count"] = 0
        symbols = list(client.symbols)
        total = len(symbols)
        runtime["scan_progress"] = f"0/{total}"
        log.info(
            "Запуск цикла сканирования %d рынков с параллельностью %d",
            total,
            cfg.scan_concurrency,
        )
        try:
            completed = 0
            progress_lock = asyncio.Lock()

            async def process_symbol(symbol: str) -> None:
                nonlocal completed
                try:
                    async with semaphore:
                        symbol_lock = symbol_locks.setdefault(
                            symbol,
                            asyncio.Lock(),
                        )
                        async with symbol_lock:
                            candles, quote_volume, current_price = (
                                await client.fetch_market(symbol)
                            )
                            events = await store.update_from_candles(
                                symbol,
                                candles,
                                current_price,
                            )
                            await deliver_signal_events(bot, cfg, store, events)
                            if events:
                                return
                            required_bars = (
                                24 * 60 // cfg.timeframe_minutes + 24
                            )
                            if (
                                store.is_active(symbol)
                                or len(candles) < required_bars
                            ):
                                return
                            evaluation = evaluate_strategy(
                                symbol,
                                candles,
                                quote_volume,
                                cfg,
                                current_price,
                            )
                            if evaluation.passed:
                                signal_now = datetime.now(timezone.utc)
                                signal_id = build_signal_id(
                                    evaluation,
                                    signal_now,
                                )
                                created_at_ms = int(
                                    signal_now.timestamp() * 1000
                                )
                                if await store.add(
                                    evaluation,
                                    signal_id,
                                    created_at_ms,
                                ):
                                    sent = await bot.send_message(
                                        cfg.telegram_chat_id,
                                        format_signal(
                                            evaluation,
                                            cfg,
                                            signal_now,
                                            signal_id,
                                        ),
                                    )
                                    await store.set_message_id(
                                        symbol,
                                        sent.message_id,
                                    )
                except asyncio.CancelledError:
                    raise
                except MarketUnavailable as exc:
                    runtime["unavailable_count"] = len(client.unavailable_symbols)
                    log.warning("%s; рынок исключён из следующих циклов", exc)
                except Exception:
                    log.exception("Ошибка обработки %s; сканирование продолжено", symbol)
                finally:
                    async with progress_lock:
                        completed += 1
                        runtime["scan_progress"] = f"{completed}/{total}"
                        if completed % 100 == 0 or completed == total:
                            log.info(
                                "Прогресс сканирования: %d/%d рынков, paused: %d",
                                completed,
                                total,
                                len(client.unavailable_symbols),
                            )

            await asyncio.gather(
                *(process_symbol(symbol) for symbol in symbols)
            )
            recovered = previously_paused - client.unavailable_symbols
            newly_paused = client.unavailable_symbols - previously_paused
            if recovered:
                log.info("Восстановлены после pause: %s", ", ".join(sorted(recovered)))
            if newly_paused:
                log.warning("Новые paused-рынки: %s", ", ".join(sorted(newly_paused)))
            added, removed = await client.refresh_markets()
            runtime["market_count"] = len(client.symbols)
            runtime["unavailable_count"] = len(client.unavailable_symbols)
            if added or removed:
                log.info(
                    "Каталог BingX обновлён: добавлено %d, удалено %d, всего %d",
                    len(added),
                    len(removed),
                    len(client.symbols),
                )
            runtime["last_scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            duration = time.monotonic() - cycle_started
            runtime["last_scan_duration"] = f"{duration:.1f} сек"
            log.info("Цикл сканирования завершён за %.1f сек", duration)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка цикла сканера")
        duration = time.monotonic() - cycle_started
        delay = cycle_delay(cfg.scanner_interval, duration, 10.0)
        runtime["next_scan_in"] = f"{delay:.1f} сек"
        await asyncio.sleep(delay)


async def monitor_active_signals(
    cfg: Config,
    client: BingXPublicClient,
    store: SignalStore,
    bot: Bot,
    runtime: dict,
    symbol_locks: dict[str, asyncio.Lock],
) -> None:
    semaphore = asyncio.Semaphore(cfg.active_monitor_concurrency)

    while True:
        cycle_started = time.monotonic()
        symbols = tracked_signal_symbols(store.active, store.pending_events)
        total = len(symbols)
        completed = 0
        progress_lock = asyncio.Lock()
        runtime["monitor_progress"] = f"0/{total}"

        async def process_symbol(symbol: str) -> None:
            nonlocal completed
            try:
                async with semaphore:
                    symbol_lock = symbol_locks.setdefault(
                        symbol,
                        asyncio.Lock(),
                    )
                    async with symbol_lock:
                        pending = [
                            event
                            for event in store.pending_events
                            if event.symbol == symbol
                        ]
                        await deliver_signal_events(
                            bot,
                            cfg,
                            store,
                            pending,
                        )
                        if not store.is_active(symbol):
                            return
                        candles, _, current_price = await client.fetch_market(
                            symbol
                        )
                        events = await store.update_from_candles(
                            symbol,
                            candles,
                            current_price,
                        )
                        await deliver_signal_events(bot, cfg, store, events)
            except asyncio.CancelledError:
                raise
            except MarketUnavailable as exc:
                log.warning(
                    "%s; активный сигнал будет проверен повторно",
                    exc,
                )
            except Exception:
                log.exception(
                    "Ошибка контроля TP/SL для %s; мониторинг продолжен",
                    symbol,
                )
            finally:
                async with progress_lock:
                    completed += 1
                    runtime["monitor_progress"] = f"{completed}/{total}"

        if symbols:
            await asyncio.gather(
                *(process_symbol(symbol) for symbol in symbols)
            )

        duration = time.monotonic() - cycle_started
        runtime["last_monitor"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        runtime["last_monitor_duration"] = f"{duration:.1f} сек"
        runtime["monitor_progress"] = "ожидание"
        delay = cycle_delay(cfg.active_monitor_interval, duration, 1.0)
        runtime["next_monitor_in"] = f"{delay:.1f} сек"
        await asyncio.sleep(delay)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def run() -> None:
    load_dotenv()
    cfg = Config.from_env()
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        raise ValueError("BOT_TOKEN и TELEGRAM_CHAT_ID обязательны")
    store = SignalStore(cfg.state_file, cfg.signal_valid_hours)
    await store.load()
    client = BingXPublicClient(cfg)
    bot = Bot(cfg.telegram_bot_token)
    runtime: dict = {}
    runner: web.AppRunner | None = None
    scanner: asyncio.Task | None = None
    monitor: asyncio.Task | None = None
    symbol_locks: dict[str, asyncio.Lock] = {}
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
        scanner = asyncio.create_task(
            scan_forever(
                cfg,
                client,
                store,
                bot,
                runtime,
                symbol_locks,
            )
        )
        monitor = asyncio.create_task(
            monitor_active_signals(
                cfg,
                client,
                store,
                bot,
                runtime,
                symbol_locks,
            )
        )
        await bot.set_my_commands([
            BotCommand(command="analyze", description="Анализ выбранной монеты"),
            BotCommand(command="scan", description="Короткая команда анализа"),
            BotCommand(command="status", description="Состояние сканера"),
            BotCommand(command="settings", description="Настройки стратегии"),
            BotCommand(command="help", description="Справка"),
            BotCommand(command="start", description="Запуск бота"),
        ])
        dispatcher = build_dispatcher(cfg, store, runtime, client)
        await dispatcher.start_polling(bot)
    finally:
        if scanner:
            scanner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scanner
        if monitor:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
        if runner:
            await runner.cleanup()
        await client.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
