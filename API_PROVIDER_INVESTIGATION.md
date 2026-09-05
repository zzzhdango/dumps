# Binance на Railway и тест WEEX

## Краткий вывод

Публичные API-ключи здесь ни при чём. Публичные методы загрузки рынков и свечей
не требуют ключа, а Binance Futures отклоняет запрос раньше аутентификации по
геолокации исходящего IP Railway или другого облачного хостинга.

В облачном тесте `GET https://fapi.binance.com/fapi/v1/exchangeInfo` вернул
HTTP 451 и текст `Service unavailable from a restricted location`. Тот же
запрос через CCXT завершился `ExchangeNotAvailable`.

WEEX в той же среде ответил без ключей:

- `GET https://api-contract.weex.com/capi/v3/market/exchangeInfo`: HTTP 200;
- `GET https://api-contract.weex.com/capi/v2/market/candles`: HTTP 200;
- загрузка рынков, свечей и тикера через CCXT 4.5.77: успешно.

## Почему Binance не работает

Официальный REST-домен USDT-M Futures:
`https://fapi.binance.com`. Он применяет региональные ограничения по IP даже
к публичным методам. Поэтому добавление или замена API-ключа не исправляет
HTTP 451.

Официальный домен `https://data-api.binance.vision` доступен без ключа, но
документирован только для Spot API `/api/v3`. Он не является заменой
`/fapi/v1` и не даёт требуемый список USDT-M perpetual futures.

Домены `fapi1.binance.com`–`fapi4.binance.com` в тесте вернули пустой HTTP 202,
а не рыночные данные, поэтому использовать их как рабочий обход нельзя.

Static Outbound IP в Railway делает адрес стабильным, но не делает его
автоматически допустимым для Binance. Railway также предупреждает, что такой
адрес может быть общим с другими клиентами и меняется при смене региона.

## Результат проверки WEEX

CCXT 4.5.4 не содержал exchange id `weex`. В CCXT 4.5.77 поддержка уже есть,
поэтому зависимость проекта обновлена.

По результату теста:

- CCXT загрузил 1 023 линейных USDT swap-рынка WEEX;
- после исключения акций, индексов и других TradFi-инструментов осталось
  577 криптовалютных perpetual-рынков;
- 454 базовых актива совпали с BingX;
- 123 базовых актива были на WEEX, но отсутствовали в текущем списке BingX.

Количество рынков меняется со временем. Бот должен обновлять список через
`load_markets(reload=True)` и повторно применять фильтр после каждого цикла.

## Ограничения полного сканирования WEEX

WEEX возвращает текущие лимиты в `rateLimits` метода `exchangeInfo` и в
заголовках `X-USED-WEIGHT-*` / `X-REMAINING-WEIGHT-*`. На момент теста
`exchangeInfo` показывал лимит 500 единиц, а запрос свечей имел вес 1.

Для полного цикла по всем криптовалютным контрактам нужно:

1. оставить `enableRateLimit=True`;
2. ограничить параллелизм;
3. учитывать заголовок оставшегося веса;
4. после HTTP 429 остановить запросы минимум на срок блокировки;
5. использовать exponential backoff с jitter;
6. сначала получать общий список тикеров и запрашивать свечи только для
   ликвидных или быстро растущих кандидатов.

Последний пункт особенно важен: запрос свечей для всех 577 рынков каждый цикл
дороже, чем двухэтапный сканер `all tickers -> shortlist -> OHLCV`.

## Как проверить внутри Railway

Откройте Shell нужного сервиса после деплоя и выполните:

```bash
python check_exchanges.py
```

Скрипт не использует секреты. Он проверит:

- BingX public swap;
- Binance USDT-M Futures;
- WEEX USDT-M perpetual;
- число доступных для сканирования рынков;
- три свечи BTC/USDT на 15m;
- текущий тикер;
- наличие ошибки restricted-location HTTP 451.

Для отдельной проверки текущего BingX-клиента:

```bash
python check_api.py
```

## Рекомендуемая архитектура

Не следует смешивать свечи BingX, объём Binance и цену WEEX в одном сигнале.
Все показатели одного сигнала должны рассчитываться по одной бирже.

Безопасная схема расширения:

1. BingX оставить основным источником.
2. WEEX добавить вторым независимым сканером.
3. Сигнал маркировать названием биржи.
4. Дедупликацию вести по ключу `exchange:symbol`.
5. Если монета есть на обеих биржах, выбрать одну по 24-часовому обороту или
   формировать два независимых анализа.
6. Binance включать только если `check_exchanges.py` внутри конкретного
   Railway-региона стабильно возвращает HTTP 200 и использование сервиса
   соответствует условиям Binance.

## Официальные материалы

- Binance USDS Futures:
  https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info
- Binance Market Data Only для Spot:
  https://developers.binance.com/en/docs/products/spot/faqs/market_data_only
- WEEX exchangeInfo:
  https://www.weex.com/api-doc/contract/Market_API/GetContractInfo
- WEEX candles:
  https://www.weex.com/api-doc/contract/V2/Market_API/GetKLineData
- WEEX access restrictions:
  https://www.weex.com/api-doc/contract/QuickStart/AccessRestrictions
- Railway Static Outbound IP:
  https://docs.railway.com/networking/static-outbound-ips
