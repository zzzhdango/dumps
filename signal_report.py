from __future__ import annotations

from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

from config import Config
from signals import SignalEvent
from strategy import StrategyEvaluation


def _compact_money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def _age(hours: float) -> str:
    return f"{round(hours * 60):d}мин" if hours < 3 else f"{hours:.1f}ч"


def _price(value: float) -> str:
    absolute = abs(value)
    if absolute >= 100:
        decimals = 2
    elif absolute >= 1:
        decimals = 4
    elif absolute >= 0.01:
        decimals = 6
    elif absolute >= 0.001:
        decimals = 7
    elif absolute >= 0.0001:
        decimals = 8
    else:
        decimals = 10
    return f"{value:.{decimals}f}"


def build_signal_id(ev: StrategyEvaluation, now: datetime | None = None) -> str:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    kyiv = now_utc.astimezone(ZoneInfo("Europe/Kyiv"))
    base = ev.symbol.split("/")[0]
    return f"{kyiv:%Y%m%d}-{base}-{kyiv:%H%M}"


def format_signal(
    ev: StrategyEvaluation,
    cfg: Config,
    now: datetime | None = None,
    signal_id: str | None = None,
) -> str:
    lv = ev.levels
    if lv is None:
        raise ValueError("Нельзя форматировать отрицательную оценку")
    m = ev.metrics
    base = ev.symbol.split("/")[0]
    current_price = m.get("current_price", lv.entry)
    deviation = (current_price / lv.entry - 1) * 100
    zone_low = lv.entry * (1 - cfg.entry_zone_pct / 100)
    zone_high = lv.entry * (1 + cfg.entry_zone_pct / 100)
    super_pump = max(m["change_1h_pct"], m["change_4h_pct"], m["change_24h_pct"]) >= cfg.super_pump_pct
    long_pump = m["pump_start_hours_ago"] >= cfg.long_pump_hours
    pump_windows = [
        f"{label}: {m[key]:+.1f}%"
        for label, key, threshold in (
            ("1h", "change_1h_pct", cfg.pump_1h_pct),
            ("4h", "change_4h_pct", cfg.pump_4h_pct),
            ("24h", "change_24h_pct", cfg.pump_24h_pct),
        )
        if m[key] >= threshold
    ]
    rsi_1h = m.get("rsi_1h", math.nan)
    rsi_text = "н/д" if math.isnan(rsi_1h) else f"{rsi_1h:.1f}"
    structure = [
        (
            f"Пик старый ({_age(m['peak_hours_ago'])})"
            if m["peak_hours_ago"] >= 1
            else f"Пик свежий ({_age(m['peak_hours_ago'])})"
        ),
        f"Откат сильный ({m['retracement_pct']:.1f}%)",
    ]
    if not math.isnan(rsi_1h):
        structure.append(
            f"RSI 1h не перегрет ({rsi_1h:.1f}) → возможен ещё рост"
            if rsi_1h < cfg.min_rsi_15m
            else f"RSI 1h высокий ({rsi_1h:.1f})"
        )
    structure.append(
        "Объём упал значительно"
        if m["current_volume_ratio"] < 0.5
        else f"Текущий объём: {m['current_volume_ratio']:.2f}x от среднего"
    )
    structure.append(
        f"Долгий памп ({m['pump_start_hours_ago']:.1f}ч)"
        if long_pump
        else f"Быстрый памп ({m['pump_start_hours_ago']:.1f}ч)"
    )

    lines = [
        f"🚀 Токен {base} ⚡ [FUTURES]",
        "",
        "🐌 ДОЛГИЙ ПАМП" if long_pump else "⚡ БЫСТРЫЙ ПАМП",
        "",
        "📍 Направление: SHORT",
        f"💰 Сигнальная цена: {_price(lv.entry)}",
        f"📡 Текущая цена: {_price(current_price)}",
        f"📉 Отклонение от сигнала: {deviation:+.2f}%",
        "",
        f"✅ Откат {m['retracement_pct']:.1f}% от пика",
        f"⏰ Пик был {_age(m['peak_hours_ago'])} назад",
        f"🕐 Памп начался: {_age(m['pump_start_hours_ago'])} назад",
    ]
    if super_pump:
        lines.append("⚡ СУПЕР-ПАМП!")
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📌 ЗОНА 1 ✅ Предпочтительнее",
        f"💰 Набор: {_price(zone_low)} - {_price(zone_high)}",
        f"🎯 ТП1 (40%): {_price(lv.tp1)} (-{cfg.tp1_pct:.1f}%)",
        f"🎯 ТП2 (30%): {_price(lv.tp2)} (-{cfg.tp2_pct:.1f}%)",
        f"🎯 ТП3 (30%): {_price(lv.tp3)} (-{cfg.tp3_pct:.1f}%)",
        f"🛑 СЛ: {_price(lv.sl)}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"⚡ Риск на сделку: {cfg.risk_pct:g}%",
        f"📊 Объём 24ч: {_compact_money(m['quote_volume_24h'])}",
        f"📈 Памп: {' | '.join(pump_windows)}",
        f"📈 RSI: {cfg.timeframe}: {m['rsi_15m']:.1f} | 1h: {rsi_text}",
        "",
        "💡 Оценка текущей структуры:",
        *[f"   • {item}" for item in structure],
    ]
    if lv.position_notional is not None:
        lines += [
            "",
            f"💵 Размер позиции (консервативно): {lv.position_notional:.2f} USDT",
            f"🪙 Количество: {lv.position_quantity:.8g} {base}",
            f"🔒 Требуемая маржа: {lv.margin_required:.2f} USDT",
        ]
    return "\n".join(lines)


def format_signal_event(event: SignalEvent) -> str:
    event_time = datetime.fromtimestamp(
        event.event_timestamp / 1000, tz=timezone.utc
    ).astimezone(ZoneInfo("Europe/Kyiv"))
    base = event.symbol.split("/")[0]
    change_pct = (event.level_price / event.entry - 1) * 100
    signal_ref = event.signal_id or "ID не сохранён"

    if event.kind == "SL":
        return "\n".join([
            "🛑 СТОП-ЛОСС СРАБОТАЛ",
            "",
            f"🚀 Токен {base} ⚡ [FUTURES]",
            f"🆔 Исходный сигнал: {signal_ref}",
            "📍 Направление: SHORT",
            f"❌ Уровень SL: {_price(event.level_price)}",
            f"📈 Изменение от сигнальной цены: {change_pct:+.2f}%",
            f"⏰ Сработал: {event_time:%H:%M:%S} (Киев)",
            "",
            "🏁 Сигнал закрыт по стоп-лоссу.",
        ])

    allocations = {"TP1": 40, "TP2": 30, "TP3": 30}
    if event.kind not in allocations:
        raise ValueError(f"Неизвестный тип события: {event.kind}")
    closing_text = (
        "🏁 Все цели по сигналу выполнены."
        if event.kind == "TP3"
        else "➡️ Сигнал остаётся активным до TP3, SL или истечения срока."
    )
    return "\n".join([
        f"🎯 {event.kind} ВЗЯТ",
        "",
        f"🚀 Токен {base} ⚡ [FUTURES]",
        f"🆔 Исходный сигнал: {signal_ref}",
        "📍 Направление: SHORT",
        f"✅ Тейк-профит: {event.kind} ({allocations[event.kind]}%)",
        f"💰 Уровень: {_price(event.level_price)}",
        f"📉 Результат от сигнальной цены: {change_pct:+.2f}%",
        f"⏰ Сработал: {event_time:%H:%M:%S} (Киев)",
        "",
        closing_text,
    ])
