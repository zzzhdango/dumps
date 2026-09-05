from datetime import datetime, timezone

from config import Config
from signal_report import build_signal_id, format_signal, format_signal_event
from signals import SignalEvent
from strategy import SignalLevels, StrategyEvaluation


def test_signal_matches_confirmed_zone_one_math_and_layout():
    entry = 0.181560
    cfg = Config(risk_pct=1.5)
    levels = SignalLevels(
        entry=entry,
        tp1=entry * 0.945,
        tp2=entry * 0.90,
        tp3=entry * 0.85,
        sl=entry * 1.03 * 1.08,
        leverage=3,
        position_notional=None,
        position_quantity=None,
        margin_required=None,
    )
    metrics = {
        "current_price": 0.178360,
        "change_1h_pct": 4.1,
        "change_4h_pct": 18.0,
        "change_24h_pct": 64.1,
        "retracement_pct": 8.3,
        "peak_hours_ago": 175 / 60,
        "pump_start_hours_ago": 6.8,
        "rsi_15m": 61.6,
        "rsi_1h": 72.0,
        "current_volume_ratio": 0.3,
        "quote_volume_24h": 9_100_000,
    }
    evaluation = StrategyEvaluation(
        "MARSCOIN/USDT:USDT", True, 0, {}, metrics, levels, ("ok",),
    )

    text = format_signal(
        evaluation,
        cfg,
        now=datetime(2026, 9, 4, 14, 40, 3, tzinfo=timezone.utc),
    )

    assert "🚀 Токен MARSCOIN ⚡ [FUTURES]" in text
    assert "🆔 ID: 20260904-MARSCOIN-1740" in text
    assert "🐌 ДОЛГИЙ ПАМП" in text
    assert "📉 Отклонение от сигнала: -1.76%" in text
    assert "⏳ СРОК ГОДНОСТИ: 6.0ч (до 23:40 Киев)" in text
    assert "💰 Набор: 0.176113 - 0.187007" in text
    assert "🎯 ТП1 (40%): 0.171574 (-5.5%)" in text
    assert "🎯 ТП2 (30%): 0.163404 (-10.0%)" in text
    assert "🎯 ТП3 (30%): 0.154326 (-15.0%)" in text
    assert "🛑 СЛ: 0.201967" in text
    assert "🔥 ЗОНА 2" not in text
    assert "Не финансовая рекомендация" not in text


def test_explicit_signal_id_and_event_messages_are_linked():
    cfg = Config()
    entry = 100.0
    levels = SignalLevels(entry, 94.5, 90, 85, 111.24, 3, None, None, None)
    evaluation = StrategyEvaluation(
        "BTC/USDT:USDT",
        True,
        0,
        {},
        {
            "current_price": 100,
            "change_1h_pct": 10,
            "change_4h_pct": 20,
            "change_24h_pct": 50,
            "retracement_pct": 5,
            "peak_hours_ago": 1,
            "pump_start_hours_ago": 6,
            "rsi_15m": 75,
            "rsi_1h": 70,
            "current_volume_ratio": 0.4,
            "quote_volume_24h": 5_000_000,
        },
        levels,
        ("ok",),
    )
    now = datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)
    assert build_signal_id(evaluation, now) == "20260905-BTC-1200"
    assert "🆔 ID: custom-id" in format_signal(evaluation, cfg, now, "custom-id")

    tp_event = SignalEvent(
        "custom-id:TP1", "TP1", evaluation.symbol, "custom-id",
        entry, 94.5, int(now.timestamp() * 1000), 777,
    )
    tp_text = format_signal_event(tp_event)
    assert "🎯 TP1 ВЗЯТ" in tp_text
    assert "🆔 Исходный сигнал: custom-id" in tp_text
    assert "✅ Тейк-профит: TP1 (40%)" in tp_text
    assert "📉 Результат от сигнальной цены: -5.50%" in tp_text
    assert "Сигнал остаётся активным" in tp_text

    sl_event = SignalEvent(
        "custom-id:SL", "SL", evaluation.symbol, "custom-id",
        entry, 111.24, int(now.timestamp() * 1000), 777,
    )
    sl_text = format_signal_event(sl_event)
    assert "🛑 СТОП-ЛОСС СРАБОТАЛ" in sl_text
    assert "📈 Изменение от сигнальной цены: +11.24%" in sl_text
    assert "Сигнал закрыт по стоп-лоссу" in sl_text
