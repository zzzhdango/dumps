from __future__ import annotations

import math

from config import Config
from strategy import StrategyEvaluation

CRITERION_NAMES = {
    "pump": "Памп окна",
    "quote_volume": "Объём 24ч",
    "rsi_or_super_pump": "RSI",
    "peak_distance": "Близко к пику",
    "retracement": "Откат",
    "volume_spike": "Объёмный всплеск",
    "price_not_rising": "Цена не растёт сейчас",
    "recent_volume_cooling": "Объём не растёт",
}


def _money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"


def _ago(hours: float) -> str:
    if hours < 2:
        return f"{round(hours * 60):d}мин назад"
    return f"{hours:.1f}ч назад"


def _yes_no(value: bool) -> str:
    return "✅" if value else "❌"


def _failed_reason(name: str, ev: StrategyEvaluation, cfg: Config) -> str:
    m = ev.metrics
    reasons = {
        "pump": "Ни одно окно пампа не достигло заданного порога.",
        "quote_volume": f"Объём 24ч ниже {_money(cfg.min_quote_volume_24h)}.",
        "rsi_or_super_pump": f"RSI недостаточно высокий, требуется ≥ {cfg.min_rsi_15m:g}.",
        "peak_distance": f"Слишком далеко от пика, больше {cfg.max_peak_distance_pct:g}%.",
        "retracement": (
            f"Откат от пика слишком маленький, меньше {cfg.min_retracement_pct:g}%. "
            f"Цена для {cfg.min_retracement_pct:g}% от пика: "
            f"{m['peak'] * (1 - cfg.min_retracement_pct / 100):.8g}."
        ),
        "volume_spike": f"Не было всплеска объёма ≥ {cfg.min_volume_ratio:g}x.",
        "price_not_rising": "Цена растёт прямо сейчас, памп может продолжаться.",
        "recent_volume_cooling": (
            f"Объём снова растёт ({m['recent_volume_ratio']:.2f}x), "
            "памп может продолжаться."
        ),
    }
    return reasons.get(name, CRITERION_NAMES.get(name, name))


def format_analysis(ev: StrategyEvaluation, cfg: Config) -> str:
    m = ev.metrics
    base = ev.symbol.split("/")[0]

    passed_windows = []
    for label, key, threshold in (
        ("1h", "change_1h_pct", cfg.pump_1h_pct),
        ("4h", "change_4h_pct", cfg.pump_4h_pct),
        ("24h", "change_24h_pct", cfg.pump_24h_pct),
    ):
        if m[key] >= threshold:
            passed_windows.append(f"{label} {m[key]:+.1f}%")
    windows_text = " | ".join(passed_windows) if passed_windows else "нет"
    super_pump = max(m["change_1h_pct"], m["change_4h_pct"], m["change_24h_pct"]) >= cfg.super_pump_pct
    rsi_1h = "н/д" if math.isnan(m["rsi_1h"]) else f"{m['rsi_1h']:.1f}"

    criteria = ev.criteria
    lines = [
        f"🔎 Анализ {base} ⚡ [FUTURES]",
        f"🪙 Цена: {m['close']:.8g}",
        "",
        "📊 Изменения:",
        (
            f"• 1h: {m['change_1h_pct']:+.1f}%  |  "
            f"4h: {m['change_4h_pct']:+.1f}%  |  "
            f"24h: {m['change_24h_pct']:+.1f}%"
        ),
        f"• Окна пройдены: {windows_text}",
        "",
        "🧠 Состояние пампа:",
        f"• Памп начался: {_ago(m['pump_start_hours_ago'])}",
        f"• Пик был: {_ago(m['peak_hours_ago'])}",
        (
            f"• Откат от пика: {m['retracement_pct']:.1f}% "
            f"({cfg.min_retracement_pct:g}% от пика = "
            f"{m['peak'] * (1 - cfg.min_retracement_pct / 100):.8g})"
        ),
        f"• Дистанция до пика ({cfg.timeframe} max): {m['peak_distance_pct']:.1f}%",
        (
            "• ⚡ Супер-памп: ДА (RSI может игнорироваться)"
            if super_pump
            else "• ⚡ Супер-памп: НЕТ"
        ),
        "",
        "📈 RSI:",
        f"• {cfg.timeframe}: {m['rsi_15m']:.1f}  |  1h: {rsi_1h}",
        "",
        "🟤 Объёмы:",
        f"• 24h объём: {_money(m['quote_volume_24h'])}",
        f"• Сейчас ({cfg.timeframe}): {m['current_volume_ratio']:.2f}x от среднего",
        f"• Макс. всплеск за 2ч: {m['max_volume_ratio']:.2f}x от среднего",
        (
            f"• Объём последние {cfg.timeframe}: {m['recent_volume_ratio']:.2f}x "
            "(3 свечи vs 3 свечи)"
        ),
        "",
        "✅ Проверки (как у бота):",
        f"• Памп окна: {_yes_no(criteria['pump'].passed)}",
        (
            f"• Объём 24ч ≥ {_money(cfg.min_quote_volume_24h)}: "
            f"{_yes_no(criteria['quote_volume'].passed)}"
        ),
        (
            f"• RSI: {_yes_no(criteria['rsi_or_super_pump'].passed)}"
            + (" (супер-памп)" if super_pump else "")
        ),
        (
            f"• Близко к пику (≤{cfg.max_peak_distance_pct:g}%): "
            f"{_yes_no(criteria['peak_distance'].passed)}"
        ),
        (
            f"• Откат ≥ {cfg.min_retracement_pct:g}%: "
            f"{_yes_no(criteria['retracement'].passed)} "
            f"(цена {cfg.min_retracement_pct:g}% от пика: "
            f"{m['peak'] * (1 - cfg.min_retracement_pct / 100):.8g})"
        ),
        (
            f"• Объём (всплеск за 2ч) ≥{cfg.min_volume_ratio:g}x: "
            f"{_yes_no(criteria['volume_spike'].passed)}"
        ),
        f"• Цена не растёт сейчас: {_yes_no(criteria['price_not_rising'].passed)}",
        (
            f"• Объём не растёт (не выше {cfg.max_recent_volume_ratio:g}x): "
            f"{_yes_no(criteria['recent_volume_cooling'].passed)}"
        ),
        "",
    ]

    if ev.passed:
        lines.append("🚀 Итог: ✅ Бот бы дал сигнал")
        if ev.levels is not None:
            lv = ev.levels
            lines += [
                "",
                "━━━━━━━━━━━━━━━━━━━━",
                "🎯 ТОРГОВЫЕ УРОВНИ",
                f"➡️ Вход: {lv.entry:.8g}",
                f"🟢 TP1: {lv.tp1:.8g} (-{cfg.tp1_pct:g}%)",
                f"🟢 TP2: {lv.tp2:.8g} (-{cfg.tp2_pct:g}%)",
                f"🟢 TP3: {lv.tp3:.8g} (-{cfg.tp3_pct:g}%)",
                f"🛑 SL: {lv.sl:.8g} (+{cfg.sl_pct:g}%)",
                f"⚙️ Плечо: {lv.leverage}x",
            ]
    else:
        lines += ["🚫 Итог: ❌ Сигнала бы не было", "Почему:"]
        lines.extend(
            f"• {_failed_reason(name, ev, cfg)}"
            for name, result in criteria.items()
            if not result.passed
        )

    return "\n".join(lines)
