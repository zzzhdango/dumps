from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message

from analysis_report import format_analysis
from config import Config
from exchange import BingXPublicClient, MarketUnavailable
from signals import SignalStore
from strategy import StrategyEvaluation, evaluate_strategy

log = logging.getLogger(__name__)


def format_signal(ev: StrategyEvaluation) -> str:
    lv = ev.levels
    if lv is None:
        raise ValueError("Нельзя форматировать отрицательную оценку")
    lines = [
        "СИГНАЛ SHORT",
        f"Инструмент: {ev.symbol}",
        f"Вход: {lv.entry:.8g}",
        f"TP1: {lv.tp1:.8g} ({(lv.tp1/lv.entry-1)*100:.2f}%)",
        f"TP2: {lv.tp2:.8g} ({(lv.tp2/lv.entry-1)*100:.2f}%)",
        f"TP3: {lv.tp3:.8g} ({(lv.tp3/lv.entry-1)*100:.2f}%)",
        f"SL: {lv.sl:.8g} (+{(lv.sl/lv.entry-1)*100:.2f}%)",
        f"Плечо: {lv.leverage}x",
    ]
    if lv.position_notional is not None:
        lines += [f"Размер позиции: {lv.position_notional:.2f} USDT ({lv.position_quantity:.8g} {ev.symbol.split('/')[0]})",
                  f"Требуемая маржа: {lv.margin_required:.2f} USDT"]
    lines += ["Причины:", *[f"• {reason}" for reason in ev.reasons],
              "Только информационный сигнал, не финансовая рекомендация."]
    return "\n".join(lines)


def build_dispatcher(
    cfg: Config,
    store: SignalStore,
    runtime: dict,
    client: BingXPublicClient,
) -> Dispatcher:
    router = Router()

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(
            "BingX Short Bot запущен.\n\n"
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
            "/analyze BTC\n"
            "/analyze BTCUSDT\n"
            "/analyze BTC/USDT:USDT\n\n"
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

    @router.message(Command("analyze", "scan"))
    async def analyze(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("Укажите монету. Пример: /analyze BTC")
            return
        query = parts[1].strip()
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
                "Проверьте тикер, например: /analyze BTC"
            )
            return
        await message.answer(f"Анализирую {symbol} по закрытым свечам BingX…")
        try:
            candles, quote_volume = await client.fetch_market(symbol)
            required_bars = 24 * 60 // cfg.timeframe_minutes + 24
            if len(candles) < required_bars:
                await message.answer(
                    f"Для {symbol} пока недостаточно истории: "
                    f"{len(candles)} из {required_bars} закрытых свечей."
                )
                return
            evaluation = evaluate_strategy(symbol, candles, quote_volume, cfg)
            await message.answer(format_analysis(evaluation, cfg.timeframe))
        except MarketUnavailable:
            await message.answer(f"{symbol} сейчас находится в состоянии paused на BingX. Попробуйте позже.")
        except Exception:
            log.exception("Ошибка ручного анализа %s", symbol)
            await message.answer(
                f"Не удалось проанализировать {symbol} из-за временной ошибки BingX. Попробуйте позже."
            )

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
