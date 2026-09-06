# Binance Futures Short Bot: архитектура и deployment

## Назначение

Сервис читает только публичные Binance USDT-M Futures candles/ticker,
оценивает SHORT-стратегию и отправляет информационные сообщения в Telegram.
Торговых операций и приватной аутентификации нет.

## Поток данных

1. `BinanceFuturesPublicClient.initialize()` загружает активные линейные
   USDT-settled perpetual swaps.
2. `scan_forever()` берёт снимок `list(client.symbols)`, загружает завершённые
   свечи и ticker, обновляет TP/SL, затем оценивает стратегию.
3. После каждого полного цикла `load_markets(reload=True)` обновляет каталог.
4. `SignalStore.prune_active_symbols()` удаляет active-сигналы отсутствующих
   рынков. State schema явно фиксирует provider `binanceusdm`.
5. `monitor_active_signals()` раз в 60 секунд сначала доставляет persistent
   pending-события, затем проверяет наличие active-рынка и только после этого
   обращается к Binance.

State/dedup и общий Telegram outbox записываются атомарно через временный файл
и `os.replace`. Deploy и runtime используют один строгий validator schema v2:
точный набор ключей, строгие bool/timestamps и проверка nullable/optional
полей. Повреждённый текущий schema v2 останавливает startup с явной ошибкой.
Неверсионированный state считается legacy BingX:
active-сигналы помещаются в quarantine и не продолжаются по Binance-ценам,
а уже созданные корректные pending-события сохраняются с исходным provider.
Повреждённые legacy/unknown записи quarantine-ятся поштучно.
Доставка outbox имеет at-least-once семантику.

## Обработка API

Клиент создаётся как `ccxt.async_support.binanceusdm` с
`enableRateLimit=True` и timeout из `REQUEST_TIMEOUT_MS`. Ключи не передаются.

Повторяемые ошибки: `NetworkError`, `RequestTimeout`,
`ExchangeNotAvailable`, `DDoSProtection`, `RateLimitExceeded`. Между попытками
используется exponential backoff. HTTP 429/418 включает общий monotonic
cooldown gate для всех конкурентных запросов. Override
`binanceusdm.handle_errors` прикрепляет status и копию response headers к
exception конкретного запроса; `Retry-After` не читается из общего
`last_response_headers`. HTTP 418 без usable header получает консервативную
паузу.
HTTP 451/restricted location немедленно преобразуется в
`BinanceRestrictedLocation`, supervisor отменяет workers и процесс
завершается ненулевым кодом. Ошибки
неизвестного/делистнутого symbol преобразуются в `MarketUnavailable`.

## Подготовка Vultr Ubuntu 24.04

Подключитесь к серверу отдельным sudo-пользователем. Установите Docker из
официального Ubuntu-репозитория:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Перезайдите в SSH, чтобы применить членство в группе `docker`. Разрешайте SSH
в firewall до включения UFW:

```bash
sudo ufw allow OpenSSH
sudo ufw enable
```

Health endpoint compose по умолчанию опубликован только на
`127.0.0.1:8080`, поэтому открывать порт 8080 наружу не нужно.

## Первый deployment

```bash
sudo git clone https://github.com/zzzhdango/dumps.git /opt/dumps
sudo chown -R "$USER":"$USER" /opt/dumps
cd /opt/dumps
cp .env.example .env
nano .env
chmod 600 .env
chmod +x deploy.sh
./deploy.sh
```

Минимально заполните:

```dotenv
BOT_TOKEN=replace_me
TELEGRAM_CHAT_ID=replace_me
ADMIN_IDS=401028479
SYMBOLS=ALL
SCANNER_INTERVAL=300
ACTIVE_MONITOR_INTERVAL=60
```

Никогда не добавляйте `.env`, Telegram token или state в Git. Публичный
Binance Futures API не требует API key/secret.

Проверка:

```bash
docker compose ps
docker compose logs --tail=200 bot
docker compose exec bot python check_api.py
curl -fsS http://127.0.0.1:8080/health
```

Если smoke возвращает HTTP 451, проблема относится к географии исходящего IP
VPS. Клиент завершает запрос сразу; изменение retry не исправит ограничение.

## State и backups

Compose использует bind volume:

```text
./data/signals_state.json -> /app/data/signals_state.json
```

До остановки контейнера `deploy.sh` выполняет schema-aware state validation,
проверяет конфликт путей, ownership/modes, собирает candidate image и
проверяет из него запись и атомарный replace в `/app/data` от UID/GID 1000.
Старый корневой `./signals_state.json` переносится в `./data` только после
короткой остановки контейнера и backup. Если существуют оба пути, создаются
conflict backups и deployment прекращается до stop. Владелец state/temp
нормализуется на UID/GID 1000, файлы получают mode 600. Для
внешнего backup сохраните обе директории. Не редактируйте state при работающем
контейнере.

## Безопасное обновление

```bash
cd /opt/dumps
git pull --ff-only
./deploy.sh
docker compose logs --tail=200 bot
```

Скрипт:

- использует lock против параллельных обновлений;
- устанавливает `.env` mode 600 и отклоняет placeholder-секреты;
- до stop проверяет compose, state schema, permissions и UID1000 writeability;
- до stop собирает candidate image;
- создаёт backup state;
- останавливает старый контейнер только перед backup/migration и заменой;
- запускает `--scale bot=1`;
- ждёт реального Docker `healthy`;
- при post-stop ошибке пытается автоматически восстановить предыдущий image,
  возвращает ненулевой код и выводит диагностику.

Rollback armed непосредственно перед `docker compose stop`. Даже если сама
команда stop вернула ошибку, trap проверяет `.State.Running`: работающий старый
контейнер не трогается, а уже остановленный восстанавливается из сохранённого
image ID.

Compose использует фиксированное имя контейнера, а процесс удерживает
singleton lock в persistent volume. Не запускайте второй каталог или прямой
`python main.py` с тем же bot token/state.

## Rollback

```bash
docker compose down
git checkout <KNOWN_GOOD_COMMIT>
cp backups/signals_state.<TIMESTAMP>.json data/signals_state.json
./deploy.sh
```

Восстанавливайте backup state только при остановленном контейнере.

## Диагностика

```bash
docker compose logs -f bot
docker compose exec bot python check_api.py
docker compose exec bot python check_exchanges.py
docker stats --no-stream
df -h
```

`check_exchanges.py` проверяет только Binance USDT-M Futures и возвращает
JSON. Архивный `API_PROVIDER_INVESTIGATION.md` не описывает текущую runtime-
архитектуру и не должен использоваться как deployment-инструкция.
