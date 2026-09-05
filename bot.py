from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from access import is_authorized
from analysis_report import format_analysis
from config import Config
from exchange import BingXPublicClient, MarketUnavailable
from signals import SignalStore
from strategy import StrategyEvaluation, evaluate_strategy

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: tuple[int, ...]) -> None:
        self.admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user_id = event.from_user.id if event.from_user else None
        if not is_authorized(user_id, self.admin_ids):
            log.warning("Отклонён доступ к боту для Telegram ID %s", user_id)
            await event.answer("❌ Извините, этот бот приватный.")
            return None
        return await handler(event, data)


def build_dispatcher(
    cfg: Config,
    store: SignalStore,
    runtime: dict,
    client: BingXPublicClient,
) -> Dispatcher:
    router = Router()
    router.message.outer_middleware(AccessMiddleware(cfg.admin_ids))

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(
            "BingX Short Bot запущен.\n\n"
            "Просто отправьте тикер монеты, например: FLOCK\n\n"
            "Команды:\n"
            "/analyze BTC — полный анализ выбранной монеты\n"
            "/scan BTC — короткий псевдоним команды /analyze\n"
            "/status — состояние сканера\n"
            "/settings — настройки стратегии\n"
            "/help — справка"
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Для ручного анализа отправьте:\n"
            "BTC\n"
            "BTCUSDT\n"
            "BTC/USDT:USDT\n\n"
            "Также доступны команды:\n"
            "/analyze BTC\n"
            "/scan BTC\n\n"
            "Бот проверит активный USDT-M фьючерс BingX по тем же критериям, "
            "что используются автоматическим сканером. API-ключ BingX не нужен."
        )

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        active = ", ".join(sorted(store.active)) or "нет"
        await message.answer(
            f"Состояние: работает\n"
            f"Рынков BingX в сканере: {runtime.get('market_count', 'инициализация')}\n"
            f"Временно paused: {runtime.get('unavailable_count', 0)}\n"
            f"Текущий прогресс: {runtime.get('scan_progress', 'ожидание')}\n"
            f"Последний цикл: {runtime.get('last_scan', 'ещё не завершён')}\n"
            f"Длительность цикла: {runtime.get('last_scan_duration', 'ещё не измерена')}\n"
            f"Активные сигналы: {active}"
        )

    @router.message(Command("settings"))
    async def settings(message: Message) -> None:
        await message.answer(
            f"Интервал: {cfg.scanner_interval} сек\nТаймфрейм: {cfg.timeframe}\nПамп: 1h {cfg.pump_1h_pct}% / 4h {cfg.pump_4h_pct}% / 24h {cfg.pump_24h_pct}%\n"
            f"RSI: {cfg.min_rsi_15m}\nМин. оборот: {cfg.min_quote_volume_24h:,.0f} USDT\nПлечо: {cfg.leverage}x\nРиск: {cfg.risk_pct}%"
        )

    async def run_analysis(message: Message, query: str) -> None:
        symbol = client.resolve_symbol(query)
        if symbol is None:
            try:
                await client.refresh_markets()
                runtime["market_count"] = len(client.symbols)
                symbol = client.resolve_symbol(query)
            except Exception:
                log.exception("Не удалось обновить каталог BingX для ручного анализа")
        if symbol is None:
            await message.answer(
                f"Активный USDT-M фьючерс «{query.upper()}» не найден на BingX. "
                "Проверьте тикер, например: BTC"
            )
            return
        try:
            candles, quote_volume, current_price = await client.fetch_market(symbol)
            required_bars = 24 * 60 // cfg.timeframe_minutes + 24
            if len(candles) < required_bars:
                await message.answer(
                    f"Для {symbol} пока недостаточно истории: "
                    f"{len(candles)} из {required_bars} закрытых свечей."
                )
                return
            evaluation = evaluate_strategy(symbol, candles, quote_volume, cfg, current_price)
            await message.answer(format_analysis(evaluation, cfg))
        except MarketUnavailable:
            await message.answer(f"{symbol} сейчас находится в состоянии paused на BingX. Попробуйте позже.")
        except Exception:
            log.exception("Ошибка ручного анализа %s", symbol)
            await message.answer(
                f"Не удалось проанализировать {symbol} из-за временной ошибки BingX. Попробуйте позже."
            )

    @router.message(Command("analyze", "scan"))
    async def analyze(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("Укажите монету. Пример: /analyze BTC")
            return
        await run_analysis(message, parts[1].strip())

    @router.message(F.text)
    async def analyze_plain_ticker(message: Message) -> None:
        query = (message.text or "").strip()
        if query.startswith("/"):
            return
        await run_analysis(message, query)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
