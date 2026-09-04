from __future__ import annotations

from dataclasses import dataclass, field
import math

import pandas as pd
from ta.momentum import RSIIndicator

from config import Config


@dataclass(frozen=True, slots=True)
class CriterionResult:
    passed: bool
    value: float | bool
    threshold: str


@dataclass(frozen=True, slots=True)
class SignalLevels:
    entry: float
    tp1: float
    tp2: float
    tp3: float
    sl: float
    leverage: int
    position_notional: float | None
    position_quantity: float | None
    margin_required: float | None


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    symbol: str
    passed: bool
    candle_timestamp: int
    criteria: dict[str, CriterionResult]
    metrics: dict[str, float]
    levels: SignalLevels | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _pct(current: float, past: float) -> float:
    return (current / past - 1.0) * 100.0 if past > 0 else math.nan


def evaluate_strategy(symbol: str, candles: pd.DataFrame, quote_volume_24h: float, cfg: Config) -> StrategyEvaluation:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(candles.columns):
        raise ValueError(f"Отсутствуют колонки: {sorted(required - set(candles.columns))}")
    bars_1h = 60 // cfg.timeframe_minutes
    bars_4h = 4 * bars_1h
    bars_24h = 24 * bars_1h
    required_bars = bars_24h + 24
    if len(candles) < required_bars:
        raise ValueError(f"Нужно минимум {required_bars} завершённых свечей {cfg.timeframe}")
    df = candles.copy().sort_values("timestamp").reset_index(drop=True)
    df = df.assign(**{
        col: pd.to_numeric(df[col], errors="coerce")
        for col in ("open", "high", "low", "close", "volume")
    })
    if df[list(required - {"timestamp"})].isna().any().any() or (df[["high", "low", "close"]] <= 0).any().any():
        raise ValueError("Свечи содержат некорректные значения")

    close = float(df.close.iloc[-1])
    changes = {
        "change_1h_pct": _pct(close, float(df.close.iloc[-(bars_1h + 1)])),
        "change_4h_pct": _pct(close, float(df.close.iloc[-(bars_4h + 1)])),
        "change_24h_pct": _pct(close, float(df.close.iloc[-(bars_24h + 1)])),
    }
    pump = (changes["change_1h_pct"] >= cfg.pump_1h_pct or
            changes["change_4h_pct"] >= cfg.pump_4h_pct or
            changes["change_24h_pct"] >= cfg.pump_24h_pct)
    super_pump = max(changes.values()) >= cfg.super_pump_pct
    rsi = float(RSIIndicator(df.close, window=14).rsi().iloc[-1])
    recent_24h = df.iloc[-bars_24h:]
    peak_index = int(recent_24h.high.idxmax())
    peak = float(df.high.loc[peak_index])
    peak_distance = (peak - close) / peak * 100.0
    retracement = peak_distance
    peak_hours_ago = max(
        0.0,
        (float(df.timestamp.iloc[-1]) - float(df.timestamp.loc[peak_index])) / 3_600_000,
    )
    before_peak = recent_24h.loc[:peak_index]
    pump_start_index = int(before_peak.low.idxmin())
    pump_start_hours_ago = max(
        0.0,
        (float(df.timestamp.iloc[-1]) - float(df.timestamp.loc[pump_start_index])) / 3_600_000,
    )

    hour_group = (df.timestamp // 3_600_000).astype("int64")
    hourly = df.groupby(hour_group).agg(close=("close", "last"), count=("close", "size"))
    hourly = hourly[hourly["count"] >= bars_1h]
    rsi_1h = (
        float(RSIIndicator(hourly.close, window=14).rsi().iloc[-1])
        if len(hourly) >= 15
        else math.nan
    )

    baseline = df.volume.rolling(20, min_periods=20).mean().shift(1)
    volume_ratios = df.volume / baseline
    bars_2h = 2 * bars_1h
    max_volume_ratio = float(volume_ratios.iloc[-bars_2h:].max())
    current_volume_ratio = float(volume_ratios.iloc[-1])
    recent_volume_ratio = float(df.volume.iloc[-3:].sum() / df.volume.iloc[-6:-3].sum()) if df.volume.iloc[-6:-3].sum() > 0 else math.inf
    price_not_rising = bool(df.close.iloc[-1] <= df.close.iloc[-2])

    criteria = {
        "pump": CriterionResult(pump, max(changes.values()), f"1h≥{cfg.pump_1h_pct}% OR 4h≥{cfg.pump_4h_pct}% OR 24h≥{cfg.pump_24h_pct}%"),
        "quote_volume": CriterionResult(quote_volume_24h >= cfg.min_quote_volume_24h, quote_volume_24h, f"≥{cfg.min_quote_volume_24h}"),
        "rsi_or_super_pump": CriterionResult(rsi >= cfg.min_rsi_15m or super_pump, rsi, f"RSI({cfg.timeframe})≥{cfg.min_rsi_15m} OR pump≥{cfg.super_pump_pct}%"),
        "peak_distance": CriterionResult(peak_distance <= cfg.max_peak_distance_pct, peak_distance, f"≤{cfg.max_peak_distance_pct}%"),
        "retracement": CriterionResult(retracement >= cfg.min_retracement_pct, retracement, f"≥{cfg.min_retracement_pct}%"),
        "volume_spike": CriterionResult(max_volume_ratio >= cfg.min_volume_ratio, max_volume_ratio, f"≥{cfg.min_volume_ratio}"),
        "price_not_rising": CriterionResult(price_not_rising, price_not_rising, "close≤previous close"),
        "recent_volume_cooling": CriterionResult(recent_volume_ratio <= cfg.max_recent_volume_ratio, recent_volume_ratio, f"≤{cfg.max_recent_volume_ratio}"),
    }
    passed = all(item.passed for item in criteria.values())
    metrics = {
        **changes,
        "quote_volume_24h": float(quote_volume_24h),
        "rsi_15m": rsi,
        "rsi_1h": rsi_1h,
        "close": close,
        "peak": peak,
        "peak_distance_pct": peak_distance,
        "retracement_pct": retracement,
        "price_5pct_from_peak": peak * 0.95,
        "pump_start_hours_ago": pump_start_hours_ago,
        "peak_hours_ago": peak_hours_ago,
        "current_volume_ratio": current_volume_ratio,
        "max_volume_ratio": max_volume_ratio,
        "recent_volume_ratio": recent_volume_ratio,
    }
    levels = None
    reasons: tuple[str, ...] = ()
    if passed:
        risk_money = cfg.account_size * cfg.risk_pct / 100.0
        notional = risk_money / (cfg.sl_pct / 100.0) if cfg.account_size > 0 else None
        quantity = notional / close if notional is not None else None
        margin = notional / cfg.leverage if notional is not None else None
        levels = SignalLevels(close, close * (1-cfg.tp1_pct/100), close * (1-cfg.tp2_pct/100),
                              close * (1-cfg.tp3_pct/100), close * (1+cfg.sl_pct/100), cfg.leverage,
                              notional, quantity, margin)
        pump_windows = [name.replace("change_", "").replace("_pct", "") for name, value in changes.items()
                        if value >= {"change_1h_pct": cfg.pump_1h_pct, "change_4h_pct": cfg.pump_4h_pct,
                                     "change_24h_pct": cfg.pump_24h_pct}[name]]
        reasons = (f"памп: {', '.join(pump_windows)}", f"RSI {cfg.timeframe}: {rsi:.1f}" if rsi >= cfg.min_rsi_15m else f"супер-памп ≥{cfg.super_pump_pct:g}%",
                   f"откат от пика: {retracement:.1f}%", f"объёмный импульс: {max_volume_ratio:.2f}x")
    return StrategyEvaluation(symbol, passed, int(df.timestamp.iloc[-1]), criteria, metrics, levels, reasons)
