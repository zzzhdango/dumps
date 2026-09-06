from __future__ import annotations

import asyncio
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
from exchange import (
    BinanceFuturesPublicClient,
    BinanceRestrictedLocation,
    MarketUnavailable,
)
from runtime_lock import RuntimeSingletonLock
from signal_report import build_signal_id, format_signal, format_signal_event
from signals import CURRENT_PROVIDER, SignalEvent, SignalStore
from scheduling import cycle_delay, tracked_signal_symbols
from strategy import evaluate_strategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


async def send_signal_event(bot: Bot, chat_id: str, event: SignalEvent) -> int:
    text = event.text if event.kind == "INITIAL" else format_signal_event(event)
    if not text:
        raise ValueError(f"Outbox event {event.event_id} has no message text")
    if event.provider != CURRENT_PROVIDER:
        text = (
            f"⚠️ Архивное уведомление провайдера {event.provider}; "
            "это не новый сигнал Binance Futures.\n\n"
            f"{text}"
        )
    if event.telegram_message_id is None:
        sent = await bot.send_message(chat_id, text)
        return sent.message_id
    try:
        sent = await bot.send_message(
            chat_id,
            text,
            reply_parameters=ReplyParameters(message_id=event.telegram_message_id),
        )
        return sent.message_id
    except TelegramBadRequest:
        log.warning(
            "Не удалось ответить на исходное сообщение %s; событие %s отправляется отдельно",
            event.telegram_message_id,
            event.event_id,
        )
        sent = await bot.send_message(chat_id, text)
        return sent.message_id


async def deliver_signal_events(
    bot: Bot,
    cfg: Config,
    store: SignalStore,
    events: list[SignalEvent],
) -> None:
    for event in events:
        message_id = await send_signal_event(
            bot,
            cfg.telegram_chat_id,
            event,
        )
        await store.acknowledge_event(event.event_id, message_id)


async def gather_cancel_on_error(*coroutines: object) -> None:
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def mark_fatal(runtime: dict, exc: BaseException) -> None:
    runtime["fatal_error"] = str(exc)
    runtime["ready"] = False
    fatal_event = runtime.get("fatal_event")
    if fatal_event is not None:
        fatal_event.set()


def readiness_status(
    runtime: dict,
    cfg: Config,
    now: float | None = None,
) -> tuple[bool, dict]:
    current = time.monotonic() if now is None else now
    details = {
        "status": "starting",
        "exchange": "Binance Futures",
        "market_count": runtime.get("market_count", 0),
    }
    if runtime.get("fatal_error"):
        details.update(status="blocked", error=runtime["fatal_error"])
        return False, details
    required = {
        "catalog": (
            runtime.get("catalog_success_at"),
            max(600.0, cfg.scanner_interval * 3.0),
        ),
        "scanner": (
            runtime.get("scanner_success_at"),
            max(600.0, cfg.scanner_interval * 3.0),
        ),
        "monitor": (
            runtime.get("monitor_success_at"),
            max(180.0, cfg.active_monitor_interval * 3.0),
        ),
    }
    missing = [name for name, (stamp, _) in required.items() if stamp is None]
    stale = [
        name
        for name, (stamp, limit) in required.items()
        if stamp is not None and current - stamp > limit
    ]
    if runtime.get("market_count", 0) <= 0:
        missing.append("markets")
    if missing or stale:
        details.update(status="not_ready", missing=missing, stale=stale)
        return False, details
    details["status"] = "ready"
    return True, details


async def scan_forever(
    cfg: Config,
    client: BinanceFuturesPublicClient,
    store: SignalStore,
    bot: Bot,
    runtime: dict,
    symbol_locks: dict[str, asyncio.Lock],
) -> None:
    semaphore = asyncio.Semaphore(cfg.scan_concurrency)

    while True:
        cycle_started = time.monotonic()
        # Снимок защищает текущий цикл от изменения каталога при refresh.
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
            successful_market_fetches = 0
            progress_lock = asyncio.Lock()

            async def process_symbol(symbol: str) -> None:
                nonlocal completed, successful_market_fetches
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
                            successful_market_fetches += 1
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
                                initial_message = format_signal(
                                    evaluation,
                                    cfg,
                                    signal_now,
                                    signal_id,
                                )
                                if await store.add(
                                    evaluation,
                                    signal_id,
                                    created_at_ms,
                                    initial_message,
                                ):
                                    await deliver_signal_events(
                                        bot,
                                        cfg,
                                        store,
                                        store.pending_for(symbol),
                                    )
                except asyncio.CancelledError:
                    raise
                except BinanceRestrictedLocation as exc:
                    mark_fatal(runtime, exc)
                    raise
                except MarketUnavailable as exc:
                    log.warning("%s; обработка рынка пропущена", exc)
                except Exception:
                    log.exception("Ошибка обработки %s; сканирование продолжено", symbol)
                finally:
                    async with progress_lock:
                        completed += 1
                        runtime["scan_progress"] = f"{completed}/{total}"
                        if completed % 100 == 0 or completed == total:
                            log.info(
                                "Прогресс сканирования Binance Futures: %d/%d рынков",
                                completed,
                                total,
                            )

            await gather_cancel_on_error(
                *(process_symbol(symbol) for symbol in symbols)
            )
            added, removed = await client.refresh_markets()
            pruned_signals = await store.prune_active_symbols(
                client.available_symbols
            )
            runtime["market_count"] = len(client.symbols)
            runtime["catalog_success_at"] = time.monotonic()
            if added or removed:
                log.info(
                    "Каталог Binance Futures обновлён: добавлено %d, удалено %d, всего %d",
                    len(added),
                    len(removed),
                    len(client.symbols),
                )
            if pruned_signals:
                log.warning(
                    "Удалены активные сигналы вне каталога Binance Futures: %s",
                    ", ".join(sorted(pruned_signals)),
                )
            if symbols and successful_market_fetches == 0:
                raise RuntimeError(
                    "Цикл сканера не получил ни одного рынка"
                )
            runtime["last_scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            runtime["scanner_success_at"] = time.monotonic()
            duration = time.monotonic() - cycle_started
            runtime["last_scan_duration"] = f"{duration:.1f} сек"
            log.info("Цикл сканирования завершён за %.1f сек", duration)
        except asyncio.CancelledError:
            raise
        except BinanceRestrictedLocation as exc:
            mark_fatal(runtime, exc)
            raise
        except Exception:
            log.exception("Ошибка цикла сканера")
        duration = time.monotonic() - cycle_started
        delay = cycle_delay(cfg.scanner_interval, duration, 10.0)
        runtime["next_scan_in"] = f"{delay:.1f} сек"
        await asyncio.sleep(delay)


async def monitor_active_signals(
    cfg: Config,
    client: BinanceFuturesPublicClient,
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
        successful_market_fetches = 0
        progress_lock = asyncio.Lock()
        runtime["monitor_progress"] = f"0/{total}"

        async def process_symbol(symbol: str) -> None:
            nonlocal completed, successful_market_fetches
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
                        if symbol not in client.available_symbols:
                            removed = await store.prune_active_symbols(
                                client.available_symbols
                            )
                            if symbol in removed:
                                log.warning(
                                    "Активный сигнал %s удалён: рынок отсутствует "
                                    "в каталоге Binance Futures",
                                    symbol,
                                )
                            return
                        candles, _, current_price = await client.fetch_market(
                            symbol
                        )
                        successful_market_fetches += 1
                        events = await store.update_from_candles(
                            symbol,
                            candles,
                            current_price,
                        )
                        await deliver_signal_events(bot, cfg, store, events)
            except asyncio.CancelledError:
                raise
            except BinanceRestrictedLocation as exc:
                mark_fatal(runtime, exc)
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
            await gather_cancel_on_error(
                *(process_symbol(symbol) for symbol in symbols)
            )
        if symbols and successful_market_fetches == 0:
            # Pending-only/removed symbols do not require market data. Any
            # still-active symbol did, so detect a fully failed monitor cycle.
            if any(store.is_active(symbol) for symbol in symbols):
                raise RuntimeError(
                    "Цикл TP/SL не получил ни одного активного рынка"
                )

        duration = time.monotonic() - cycle_started
        runtime["last_monitor"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        runtime["last_monitor_duration"] = f"{duration:.1f} сек"
        runtime["monitor_success_at"] = time.monotonic()
        runtime["monitor_progress"] = "ожидание"
        delay = cycle_delay(cfg.active_monitor_interval, duration, 1.0)
        runtime["next_monitor_in"] = f"{delay:.1f} сек"
        await asyncio.sleep(delay)


async def health(request: web.Request) -> web.Response:
    runtime = request.app["runtime"]
    cfg = request.app["config"]
    ready, payload = readiness_status(runtime, cfg)
    return web.json_response(payload, status=200 if ready else 503)


def validate_runtime_secrets(cfg: Config) -> None:
    token = cfg.telegram_bot_token.strip()
    chat_id = cfg.telegram_chat_id.strip()
    placeholders = ("replace_me", "changeme", "example")
    if not token or any(value in token.lower() for value in placeholders):
        raise ValueError("BOT_TOKEN обязателен и не должен быть placeholder")
    if (
        not chat_id
        or any(value in chat_id.lower() for value in placeholders)
        or chat_id == "-1001234567890"
    ):
        raise ValueError(
            "TELEGRAM_CHAT_ID обязателен и не должен быть placeholder"
        )


async def supervise_tasks(tasks: dict[str, asyncio.Task]) -> None:
    done, pending = await asyncio.wait(
        tasks.values(),
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    completed: list[tuple[str, asyncio.Task]] = [
        (name, task) for name, task in tasks.items() if task in done
    ]
    for _, task in completed:
        if task.cancelled():
            raise asyncio.CancelledError
        error = task.exception()
        if error is not None:
            raise error
    if any(name == "telegram" for name, _ in completed):
        return
    name = completed[0][0]
    raise RuntimeError(f"Критический worker {name} неожиданно завершился")


async def watch_fatal(runtime: dict) -> None:
    await runtime["fatal_event"].wait()
    raise BinanceRestrictedLocation(runtime["fatal_error"])


async def run() -> None:
    load_dotenv()
    cfg = Config.from_env()
    validate_runtime_secrets(cfg)
    singleton = RuntimeSingletonLock(cfg.state_file)
    singleton.acquire()
    runtime: dict = {"fatal_event": asyncio.Event()}
    runner: web.AppRunner | None = None
    tasks: dict[str, asyncio.Task] = {}
    symbol_locks: dict[str, asyncio.Lock] = {}
    client: BinanceFuturesPublicClient | None = None
    bot: Bot | None = None
    try:
        store = SignalStore(cfg.state_file, cfg.signal_valid_hours)
        client = BinanceFuturesPublicClient(cfg)
        bot = Bot(cfg.telegram_bot_token)
        await store.load()
        await client.initialize()
        pruned_signals = await store.prune_active_symbols(
            client.available_symbols
        )
        runtime["market_count"] = len(client.symbols)
        runtime["catalog_success_at"] = time.monotonic()
        log.info(
            "Загружено активных Binance Futures USDT-M рынков: %d",
            len(client.symbols),
        )
        if pruned_signals:
            log.warning(
                "При запуске удалены активные сигналы вне криптокаталога: %s",
                ", ".join(sorted(pruned_signals)),
            )
        app = web.Application()
        app["runtime"] = runtime
        app["config"] = cfg
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, cfg.health_host, cfg.health_port).start()
        tasks["scanner"] = asyncio.create_task(
            scan_forever(
                cfg,
                client,
                store,
                bot,
                runtime,
                symbol_locks,
            )
        )
        tasks["monitor"] = asyncio.create_task(
            monitor_active_signals(
                cfg,
                client,
                store,
                bot,
                runtime,
                symbol_locks,
            )
        )
        tasks["fatal_watcher"] = asyncio.create_task(watch_fatal(runtime))
        await bot.set_my_commands([
            BotCommand(command="analyze", description="Анализ выбранной монеты"),
            BotCommand(command="scan", description="Короткая команда анализа"),
            BotCommand(command="status", description="Состояние сканера"),
            BotCommand(command="settings", description="Настройки стратегии"),
            BotCommand(command="help", description="Справка"),
            BotCommand(command="start", description="Запуск бота"),
        ])
        dispatcher = build_dispatcher(
            cfg,
            store,
            runtime,
            client,
            symbol_locks,
        )
        tasks["telegram"] = asyncio.create_task(
            dispatcher.start_polling(bot)
        )
        await supervise_tasks(tasks)
    finally:
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        if runner:
            await runner.cleanup()
        if client is not None:
            await client.close()
        if bot is not None:
            await bot.session.close()
        singleton.release()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except BinanceRestrictedLocation as exc:
        log.critical("%s; процесс остановлен", exc)
        raise SystemExit(2)
    except KeyboardInterrupt:
        pass
