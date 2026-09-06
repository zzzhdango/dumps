import inspect

from access import is_authorized
from analysis_report import format_analysis
import bot
from config import Config
from strategy import CriterionResult, SignalLevels, StrategyEvaluation


def metrics() -> dict[str, float]:
    return {
        "close": 100.0,
        "change_1h_pct": 12.5,
        "change_4h_pct": 24.0,
        "change_24h_pct": 35.0,
        "rsi_15m": 78.2,
        "rsi_1h": 72.4,
        "quote_volume_24h": 5_000_000.0,
        "peak": 106.0,
        "peak_distance_pct": 5.66,
        "retracement_pct": 5.66,
        "price_5pct_from_peak": 100.7,
        "pump_start_hours_ago": 4.9,
        "peak_hours_ago": 1.1,
        "current_volume_ratio": 0.15,
        "max_volume_ratio": 1.8,
        "recent_volume_ratio": 0.9,
    }


def criteria(passed: bool = True) -> dict[str, CriterionResult]:
    return {
        "pump": CriterionResult(passed, 35.0, "1h≥10% OR 4h≥20% OR 24h≥30%"),
        "quote_volume": CriterionResult(True, 5_000_000.0, "≥3000000"),
        "rsi_or_super_pump": CriterionResult(True, 78.2, "RSI(15m)≥75 OR pump≥50%"),
        "peak_distance": CriterionResult(True, 5.66, "≤10%"),
        "retracement": CriterionResult(True, 5.66, "≥5%"),
        "volume_spike": CriterionResult(True, 1.8, "≥1.3"),
        "price_not_rising": CriterionResult(True, True, "close≤previous close"),
        "recent_volume_cooling": CriterionResult(True, 0.9, "≤1.3"),
    }


def test_whitelist_allows_configured_admin_only():
    admins = (401028479, 987654321)
    assert is_authorized(401028479, admins)
    assert is_authorized(987654321, admins)
    assert not is_authorized(123456789, admins)
    assert not is_authorized(None, admins)


def test_formats_positive_manual_analysis():
    levels = SignalLevels(100, 94.5, 90, 85, 111.25, 3, None, None, None)
    evaluation = StrategyEvaluation(
        "BTC/USDT:USDT", True, 1_000, criteria(), metrics(), levels, ("памп",),
    )

    text = format_analysis(evaluation, Config())

    assert "🔎 Анализ BTC ⚡ [FUTURES]" in text
    assert "📈 RSI:" in text
    assert "15m: 78.2  |  1h: 72.4" in text
    assert "• Памп окна: ✅" in text
    assert "🚀 Итог: ✅ Бот бы дал сигнал" in text
    assert "🟢 TP1: 94.5" in text
    assert "🛑 SL: 111.25" in text
    assert "Срез" not in text
    assert "Не финансовая рекомендация" not in text
    assert "закрытых свечах BingX" not in text


def test_formats_negative_manual_analysis_with_failed_reasons():
    evaluation = StrategyEvaluation(
        "BTC/USDT:USDT", False, 1_000, criteria(False), metrics(), None, (),
    )

    text = format_analysis(evaluation, Config())

    assert "• Памп окна: ❌" in text
    assert "🚫 Итог: ❌ Сигнала бы не было" in text
    assert "• Ни одно окно пампа не достигло заданного порога." in text
    assert "TP1:" not in text


def test_runtime_bot_strings_are_binance_futures_only():
    source = inspect.getsource(bot)
    assert "Binance Futures" in source
    assert "BingX" not in source
    assert "paused" not in source.lower()
    assert "TradFi" not in source
