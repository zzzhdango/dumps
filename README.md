# Binance Futures Short Bot

Асинхронный Telegram-бот на Python 3.11. Он использует **только публичные
данные Binance USDT-M Futures**, ищет потенциальные SHORT-сигналы и
контролирует TP/SL. Бот не размещает ордера, не вызывает приватные endpoints и
не требует ключей Binance.

## Что сохранено

- стратегия пампа, охлаждения цены/объёма и RSI;
- формат сигналов и событий TP1/TP2/TP3/SL;
- Telegram-команды и whitelist `ADMIN_IDS`;
- persistent state, dedup по свече и доставка pending-событий после рестарта;
- provider-aware state schema и persistent outbox для исходных сигналов;
- полный скан раз в 300 секунд и монитор активных сигналов раз в 60 секунд.

## Каталог рынков

`ccxt.async_support.binanceusdm` загружает публичный каталог. Сканер принимает
только рынки, у которых:

- `swap=True`, `linear=True`, `quote=USDT`, `settle=USDT`, `active=True`;
- `info.status=TRADING`, если поле присутствует;
- `info.contractType=PERPETUAL`, если поле присутствует.

Delivery/quarterly, inverse/COIN-M, USDC и неактивные инструменты исключены.
Внутри проекта везде используются CCXT unified symbols вида
`BTC/USDT:USDT`. После каждого полного цикла каталог загружается заново с
`reload=True`; активные сигналы по исчезнувшим рынкам удаляются, но уже
сохранённые pending-события сначала доставляются.

## Команды Telegram

- `/start`
- `/help`
- `/status`
- `/settings`
- `/analyze BTC`
- `/scan BTC`

Ручной анализ принимает `BTC`, `BTCUSDT` и `BTC/USDT:USDT`.

## Быстрый запуск

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните BOT_TOKEN, TELEGRAM_CHAT_ID и ADMIN_IDS
python check_api.py
python main.py
```

`check_api.py` проверяет discovery, завершённые свечи и ticker на Binance
Futures, предпочитая `BTC/USDT:USDT`. `check_exchanges.py` — расширенный
Binance-only connectivity test с JSON-результатом. При HTTP 451 оба скрипта
явно сообщают, что исходящий IP относится к restricted location.

HTTP `/health` возвращает `200` только после свежих успешных циклов каталога,
сканера и TP/SL-монитора. Во время старта, при устаревших циклах или fatal
HTTP 451 возвращается `503`.

## Конфигурация

Критически важные переменные:

| Переменная | По умолчанию | Назначение |
|---|---:|---|
| `BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Чат для сигналов |
| `ADMIN_IDS` | `401028479` | Разрешённые Telegram user ID через запятую |
| `SYMBOLS` | `ALL` | Все рынки либо unified whitelist |
| `SCANNER_INTERVAL` | `300` | Интервал полного сканера, секунд |
| `ACTIVE_MONITOR_INTERVAL` | `60` | Интервал TP/SL-монитора, секунд |
| `SCAN_CONCURRENCY` | `5` | Параллельность сканера |
| `ACTIVE_MONITOR_CONCURRENCY` | `3` | Параллельность монитора |
| `REQUEST_TIMEOUT_MS` | `20000` | Timeout публичного API |
| `MAX_RETRIES` | `4` | Число попыток сетевых запросов |
| `RETRY_BASE_SECONDS` | `1` | База exponential backoff |
| `STATE_FILE` | `signals_state.json` | Путь persistent state |

Полный список и значения стратегии находятся в `.env.example`. Binance API
key/secret не используются и не должны добавляться.

## Надёжность

- Повторы применяются к `NetworkError`, `RequestTimeout`,
  `ExchangeNotAvailable`, `DDoSProtection` и `RateLimitExceeded`.
- Задержка растёт экспоненциально. На границе
  `binanceusdm.handle_errors` status и копия headers прикрепляются к конкретному
  exception, поэтому для HTTP 429/418 используется request-local
  `Retry-After`, а не конкурентно изменяемый `last_response_headers`.
- HTTP 451 преобразуется в `BinanceRestrictedLocation` и сразу завершает
  попытку без повторов. Ошибка поднимается до supervisor, остальные задачи
  отменяются, процесс завершается с ненулевым кодом.
- HTTP 418/429 включает общий для клиента monotonic cooldown gate: новые
  конкурентные запросы ждут окончания блокировки.
- `BadSymbol`/делистинг одного контракта преобразуется в `MarketUnavailable`
  и не останавливает общий цикл.
- В стратегию передаются только завершённые свечи, 24h `quoteVolume` и
  текущая ticker-цена.

## Vultr Ubuntu 24.04 + Docker Compose

Подробная процедура находится в `ANALYSIS_AND_DEPLOY.md`. Кратко:

```bash
cp .env.example .env
chmod 600 .env
nano .env
chmod +x deploy.sh
./deploy.sh
docker compose logs -f bot
```

Compose монтирует `./data` в `/app/data`; state хранится в
`./data/signals_state.json`. `deploy.sh` отклоняет placeholder-секреты,
до остановки работающего контейнера проверяет compose, schema state,
permissions, собирает candidate image и проверяет запись/атомарную замену
файла от UID 1000. Rollback вооружается до команды `compose stop` и при её
ошибке проверяет фактическое состояние фиксированного контейнера. Контейнер
останавливается только на время согласованного
backup/переноса legacy state и запуска уже собранного image. Post-stop ошибка
активирует rollback предыдущего image. Затем скрипт ждёт Docker healthcheck.
При наличии обоих state-файлов deployment останавливается без downtime после
создания conflict backups. Скрипт принудительно
оставляет **один** экземпляр сервиса. Не запускайте
параллельно второй compose project или `python main.py`: Telegram polling и
state рассчитаны на один экземпляр.

Дополнительно процесс удерживает `flock` на
`/app/data/.binance_futures_bot.lock`, а Compose использует фиксированное имя
контейнера. Второй процесс с тем же persistent volume завершится с ошибкой.

State schema содержит `schema_version=2` и `provider=binanceusdm`.
Единый validator для deploy preflight и runtime требует точный набор полей,
запрещает неизвестные ключи и строго проверяет bool, timestamps, nullable
Telegram message IDs и остальные optional-поля. Ошибка в текущем schema v2
завершает startup явно, вместо запуска с пустым state.
Неверсионированный state считается legacy BingX: active-сигналы не
продолжаются по Binance-ценам и переносятся в quarantine, а уже созданные
pending-уведомления сохраняются с исходным provider и доставляются без
запроса рынка с явной архивной пометкой. Повреждённые legacy/unknown entries
quarantine-ятся поштучно, не уничтожая корректные pending entries. Отправка
Telegram имеет at-least-once семантику: persistent
outbox предотвращает потерю сообщения, но crash после отправки до ack может
дать повтор.

## Проверки

```bash
python -m pytest -q
python -m compileall -q .
git diff --check
```

Unit-тесты не обращаются к сети.

## Исторический отчёт

`API_PROVIDER_INVESTIGATION.md` сохранён только как архив исследования
провайдеров. Он не является текущей инструкцией; runtime этого проекта
использует исключительно Binance USDT-M Futures.
