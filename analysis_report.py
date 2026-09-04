from __future__ import annotations

from strategy import CriterionResult, StrategyEvaluation

CRITERION_NAMES = {
    "pump": "Ценовой импульс",
    "quote_volume": "Оборот за 24 часа",
    "rsi_or_super_pump": "RSI или супер-памп",
    "peak_distance": "Расстояние до пика",
    "retracement": "Откат от пика",
    "volume_spike": "Всплеск объёма",
    "price_not_rising": "Подтверждение разворота",
    "recent_volume_cooling": "Затухание объёма",
}


def _criterion_value(name: str, result: CriterionResult) -> str:
    if isinstance(result.value, bool):
        return "да" if result.value else "нет"
    if name == "quote_volume":
        return f"{result.value:,.0f} USDT"
    if name in {"pump", "peak_distance", "retracement"}:
        return f"{result.value:.2f}%"
    if name == "rsi_or_super_pump":
        return f"{result.value:.1f}"
    return f"{result.value:.2f}x"


def format_analysis(ev: StrategyEvaluation, timeframe: str) -> str:
    m = ev.metrics
    verdict = "SHORT-СИГНАЛ НАЙДЕН" if ev.passed else "SHORT-СИГНАЛА НЕТ"
    lines = [
        f"АНАЛИЗ {ev.symbol}",
        f"Таймфрейм: {timeframe}",
        f"Последняя закрытая цена: {m['close']:.8g}",
        "",
        "Рыночные показатели:",
        f"• Изменение 1h: {m['change_1h_pct']:+.2f}%",
        f"• Изменение 4h: {m['change_4h_pct']:+.2f}%",
        f"• Изменение 24h: {m['change_24h_pct']:+.2f}%",
        f"• RSI ({timeframe}): {m['rsi_15m']:.1f}",
        f"• Оборот 24h: {m['quote_volume_24h']:,.0f} USDT",
        f"• Пик 24h: {m['peak']:.8g}",
        f"• Откат от пика: {m['retracement_pct']:.2f}%",
        f"• Максимальный всплеск объёма: {m['max_volume_ratio']:.2f}x",
        f"• Текущий коэффициент объёма: {m['recent_volume_ratio']:.2f}x",
        "",
        "Проверка критериев:",
    ]
    for name, result in ev.criteria.items():
        mark = "ДА" if result.passed else "НЕТ"
        label = CRITERION_NAMES.get(name, name)
        lines.append(f"• [{mark}] {label}: {_criterion_value(name, result)}; условие {result.threshold}")
    lines += ["", f"Итог: {verdict}"]
    if ev.levels is not None:
        lv = ev.levels
        lines += [
            f"Вход: {lv.entry:.8g}",
            f"TP1: {lv.tp1:.8g}",
            f"TP2: {lv.tp2:.8g}",
            f"TP3: {lv.tp3:.8g}",
            f"SL: {lv.sl:.8g}",
            f"Плечо: {lv.leverage}x",
        ]
    else:
        failed = [CRITERION_NAMES.get(name, name) for name, value in ev.criteria.items() if not value.passed]
        lines.append(f"Не выполнено: {', '.join(failed)}.")
    lines += ["", "Анализ основан на закрытых свечах BingX. Не финансовая рекомендация."]
    return "\n".join(lines)
