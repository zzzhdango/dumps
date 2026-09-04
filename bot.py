from __future__ import annotations

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message

from config import Config
from signals import SignalStore
from strategy import StrategyEvaluation


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


def build_dispatcher(cfg: Config, store: SignalStore, runtime: dict) -> Dispatcher:
    router = Router()

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer("BingX Short Bot запущен. Команды: /status, /settings")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        active = ", ".join(sorted(store.active)) or "нет"
        await message.answer(
            f"Состояние: работает\n"
            f"Рынков BingX в сканере: {runtime.get('market_count', 'инициализация')}\n"
            f"Последний цикл: {runtime.get('last_scan', 'ещё не завершён')}\n"
            f"Активные сигналы: {active}"
        )

    @router.message(Command("settings"))
    async def settings(message: Message) -> None:
        await message.answer(
            f"Интервал: {cfg.scanner_interval} сек\nТаймфрейм: {cfg.timeframe}\nПамп: 1h {cfg.pump_1h_pct}% / 4h {cfg.pump_4h_pct}% / 24h {cfg.pump_24h_pct}%\n"
            f"RSI: {cfg.min_rsi_15m}\nМин. оборот: {cfg.min_quote_volume_24h:,.0f} USDT\nПлечо: {cfg.leverage}x\nРиск: {cfg.risk_pct}%"
        )

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
