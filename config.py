from __future__ import annotations

import os
from dataclasses import dataclass
import re
from typing import Mapping


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} должен быть числом") from exc


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} должен быть целым числом") from exc


def _symbols(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("SYMBOLS", "").strip()
    if raw.upper() in {"", "ALL", "*"}:
        return ()
    # Защита от частой ошибки при вставке в Railway: значение "SYMBOLS=".
    if raw.upper().startswith("SYMBOLS="):
        raw = raw.split("=", 1)[1].strip()
        if raw.upper() in {"", "ALL", "*"}:
            return ()
    return tuple(item.strip().upper() for item in raw.split(",") if item.strip())


def _admin_ids(env: Mapping[str, str]) -> tuple[int, ...]:
    raw = env.get("ADMIN_IDS", "401028479").strip()
    try:
        values = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("ADMIN_IDS должен содержать Telegram ID через запятую") from exc
    if not values or any(value <= 0 for value in values):
        raise ValueError("ADMIN_IDS должен содержать хотя бы один положительный Telegram ID")
    return values


@dataclass(frozen=True, slots=True)
class Config:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    admin_ids: tuple[int, ...] = (401028479,)
    bingx_api_key: str = ""
    bingx_secret: str = ""
    scanner_interval: int = 300
    request_timeout_ms: int = 20_000
    max_retries: int = 4
    retry_base_seconds: float = 1.0
    ohlcv_limit: int = 150
    timeframe: str = "15m"
    symbols: tuple[str, ...] = ()
    state_file: str = "signals_state.json"
    health_host: str = "0.0.0.0"
    health_port: int = 8080

    pump_1h_pct: float = 10.0
    pump_4h_pct: float = 20.0
    pump_24h_pct: float = 30.0
    super_pump_pct: float = 50.0
    min_quote_volume_24h: float = 3_000_000.0
    min_rsi_15m: float = 75.0
    max_peak_distance_pct: float = 10.0
    min_retracement_pct: float = 5.0
    min_volume_ratio: float = 1.3
    max_recent_volume_ratio: float = 1.3
    tp1_pct: float = 5.5
    tp2_pct: float = 10.0
    tp3_pct: float = 15.0
    entry_zone_pct: float = 3.0
    sl_above_zone_pct: float = 8.0
    signal_valid_hours: float = 6.0
    long_pump_hours: float = 6.0
    leverage: int = 3
    account_size: float = 0.0
    risk_pct: float = 1.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        e = os.environ if env is None else env
        symbols = _symbols(e)
        cfg = cls(
            telegram_bot_token=e.get("BOT_TOKEN", e.get("TELEGRAM_BOT_TOKEN", "")).strip(),
            telegram_chat_id=e.get("TELEGRAM_CHAT_ID", "").strip(),
            admin_ids=_admin_ids(e),
            bingx_api_key=e.get("BINGX_API_KEY", "").strip(),
            bingx_secret=e.get("BINGX_SECRET", "").strip(),
            scanner_interval=_int(e, "SCANNER_INTERVAL", 300),
            request_timeout_ms=_int(e, "REQUEST_TIMEOUT_MS", 20_000),
            max_retries=_int(e, "MAX_RETRIES", 4),
            retry_base_seconds=_float(e, "RETRY_BASE_SECONDS", 1.0),
            ohlcv_limit=_int(e, "OHLCV_LIMIT", 150),
            timeframe=e.get("TIMEFRAME", "15m").strip(),
            symbols=symbols,
            state_file=e.get("STATE_FILE", "signals_state.json"),
            health_host=e.get("HEALTH_HOST", "0.0.0.0"),
            health_port=_int(e, "PORT", 8080),
            pump_1h_pct=_float(e, "PUMP_1H_PCT", 10),
            pump_4h_pct=_float(e, "PUMP_4H_PCT", 20),
            pump_24h_pct=_float(e, "PUMP_24H_PCT", 30),
            super_pump_pct=_float(e, "SUPER_PUMP_PCT", 50),
            min_quote_volume_24h=_float(e, "MIN_QUOTE_VOLUME_24H", 3_000_000),
            min_rsi_15m=_float(e, "MIN_RSI_15M", 75),
            max_peak_distance_pct=_float(e, "MAX_PEAK_DISTANCE_PCT", 10),
            min_retracement_pct=_float(e, "MIN_RETRACEMENT_PCT", 5),
            min_volume_ratio=_float(e, "MIN_VOLUME_RATIO", 1.3),
            max_recent_volume_ratio=_float(e, "MAX_RECENT_VOLUME_RATIO", 1.3),
            tp1_pct=_float(e, "TP1_PCT", 5.5),
            tp2_pct=_float(e, "TP2_PCT", 10),
            tp3_pct=_float(e, "TP3_PCT", 15),
            entry_zone_pct=_float(e, "ENTRY_ZONE_PCT", 3),
            sl_above_zone_pct=_float(e, "SL_ABOVE_ZONE_PCT", 8),
            signal_valid_hours=_float(e, "SIGNAL_VALID_HOURS", 6),
            long_pump_hours=_float(e, "LONG_PUMP_HOURS", 6),
            leverage=_int(e, "LEVERAGE", 3),
            account_size=_float(e, "ACCOUNT_SIZE", 0),
            risk_pct=_float(e, "RISK_PCT", 1),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.admin_ids or any(admin_id <= 0 for admin_id in self.admin_ids):
            raise ValueError("ADMIN_IDS должен содержать положительные Telegram ID")
        if self.scanner_interval < 10:
            raise ValueError("SCANNER_INTERVAL должен быть не меньше 10 секунд")
        if self.ohlcv_limit < 120:
            raise ValueError("OHLCV_LIMIT должен быть не меньше 120")
        match = re.fullmatch(r"([1-9]\d*)([mhd])", self.timeframe)
        if not match:
            raise ValueError("TIMEFRAME должен иметь формат 1m, 5m, 15m, 1h и т.п.")
        amount, unit = int(match.group(1)), match.group(2)
        minutes = amount * {"m": 1, "h": 60, "d": 1440}[unit]
        if minutes > 60 or 60 % minutes != 0:
            raise ValueError("TIMEFRAME должен делить 1 час без остатка и быть не больше 1h")
        minimum_bars = 24 * 60 // minutes + 24
        if self.ohlcv_limit < minimum_bars:
            raise ValueError(f"OHLCV_LIMIT должен быть не меньше {minimum_bars} для {self.timeframe}")
        if self.request_timeout_ms <= 0 or self.max_retries < 1 or self.retry_base_seconds < 0:
            raise ValueError("Некорректные параметры сетевых повторов")
        if self.health_port not in range(1, 65536):
            raise ValueError("PORT должен быть от 1 до 65535")
        positive = (self.pump_1h_pct, self.pump_4h_pct, self.pump_24h_pct,
                    self.super_pump_pct, self.min_quote_volume_24h, self.min_rsi_15m,
                    self.max_peak_distance_pct, self.min_retracement_pct,
                    self.min_volume_ratio, self.max_recent_volume_ratio,
                    self.tp1_pct, self.tp2_pct, self.tp3_pct,
                    self.entry_zone_pct, self.sl_above_zone_pct,
                    self.signal_valid_hours, self.long_pump_hours, self.risk_pct)
        if any(v <= 0 for v in positive):
            raise ValueError("Пороговые значения должны быть положительными")
        if not (self.tp1_pct < self.tp2_pct < self.tp3_pct):
            raise ValueError("Тейк-профиты должны возрастать: TP1 < TP2 < TP3")
        if self.min_retracement_pct > self.max_peak_distance_pct:
            raise ValueError("MIN_RETRACEMENT_PCT не может превышать MAX_PEAK_DISTANCE_PCT")
        if self.leverage < 1 or self.leverage > 125:
            raise ValueError("LEVERAGE должен быть от 1 до 125")
        if self.account_size < 0 or self.risk_pct > 100:
            raise ValueError("Некорректный размер счёта или риск")
        pattern = re.compile(r"^[A-Z0-9]+/USDT:USDT$")
        if any(not pattern.fullmatch(symbol) for symbol in self.symbols):
            raise ValueError("SYMBOLS должны быть в формате BTC/USDT:USDT")

    @property
    def timeframe_minutes(self) -> int:
        match = re.fullmatch(r"([1-9]\d*)([mhd])", self.timeframe)
        if not match:
            raise ValueError("Некорректный TIMEFRAME")
        return int(match.group(1)) * {"m": 1, "h": 60, "d": 1440}[match.group(2)]
