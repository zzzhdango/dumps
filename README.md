# BingX Short Bot

Асинхронный Telegram-бот на Python 3.11, который читает только публичные рыночные данные BingX perpetual swaps через `ccxt.async_support` и отправляет сигналы на потенциальный шорт. Ордеров и запросов к приватному API нет.

Binance намеренно не используется.

## Логика сигнала

Все расчёты выполняются только по завершённым свечам выбранного `TIMEFRAME` (по умолчанию 15m). Одновременно должны выполняться условия:

- рост хотя бы в одном окне: 1h ≥ 10%, 4h ≥ 20% или 24h ≥ 30%;
- 24h quote volume ≥ 3 000 000 USDT;
- RSI(14) выбранного таймфрейма ≥ 75 либо рост в любом окне ≥ 50%;
- текущая цена на 5–10% ниже максимума последних 24 часов;
- максимальное отношение объёма одной из свечей за последние 2 часа к предшествующей rolling-20 базе ≥ 1.3;
- close последней свечи не выше предыдущего close;
- объём последних трёх свечей / объём предыдущих трёх ≤ 1.3.

Вход — последний завершённый close. Цели: −5.5%, −10%, −15%; стоп: +11.25%. Повторный сигнал не отправляется, пока активный не закрыт по SL или TP1. Если одна свеча касается обоих уровней, первым считается SL.

## Локальный запуск

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните BOT_TOKEN и TELEGRAM_CHAT_ID
python check_api.py
python main.py
```

Команды Telegram: `/start`, `/status`, `/settings`. Healthcheck: `GET /health`.

`SYMBOLS` принимает unified symbols через запятую. По умолчанию указаны тикеры со скриншотов: `MARSCOIN/USDT:USDT`, `USELESS/USDT:USDT`, `SKR/USDT:USDT`, `PONS/USDT:USDT`, `FLOCK/USDT:USDT`; недоступные на момент запуска рынки пропускаются с предупреждением. Для проверки инфраструктуры можно временно задать `SYMBOLS=BTC/USDT:USDT`. `TIMEFRAME` настраивается без изменения логики; допустимы интервалы не больше 1h, которые делят час без остатка.

`ACCOUNT_SIZE=0` скрывает размер позиции. При положительном размере риск в USDT равен `ACCOUNT_SIZE × RISK_PCT / 100`; notional равен риску, делённому на относительное расстояние до SL; количество равно `notional / entry`, требуемая маржа — `notional / LEVERAGE`.

`BINGX_API_KEY` и `BINGX_SECRET` оставьте пустыми: текущая версия использует только публичные candles/ticker и не передаёт ключи в ccxt.

## Railway

1. Создайте новый Railway Project и подключите репозиторий с этой директорией.
2. Railway автоматически использует `Dockerfile` и `railway.json`.
3. В Variables добавьте `BOT_TOKEN`, `TELEGRAM_CHAT_ID` и нужные параметры из `.env.example`.
4. Добавьте persistent Volume и смонтируйте его, например, в `/data`; установите `STATE_FILE=/data/signals_state.json`.
5. Railway задаёт `PORT` автоматически. Сервис должен иметь публичный domain, чтобы Railway мог вызвать `GET /health`.
6. Для smoke-проверки откройте Railway Shell и выполните `SYMBOLS=BTC/USDT:USDT python check_api.py`. Ожидается строка `OK: BingX public API...`.

Это веб-сервис с фоновым scanner task, а не отдельный worker: один процесс одновременно держит HTTP healthcheck и long polling Telegram. Не масштабируйте сервис больше чем до одной реплики, иначе несколько poller-процессов будут конкурировать.

## Тесты

```bash
pytest -q
```

Тесты не обращаются к сети: стратегия получает искусственные DataFrame, жизненный цикл сигналов использует временный JSON-файл.
