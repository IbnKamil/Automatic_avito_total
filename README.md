# Avito Monitor — мониторинг скамеек на Авито

Программа сканирует объявления на [Авито](https://www.avito.ru) по заданному товару и региону, сохраняет историю, строит аналитику рынка с графиками и отправляет HTML-отчёт на email **каждые 3 дня**.

По умолчанию:
- **Товар:** Скамейки
- **Регион:** Дагестан (`dagestan`, `location_id=646710`)

## Возможности

- Сканирование всех страниц результатов поиска на Авито
- Сохранение истории в SQLite для отслеживания динамики
- Аналитика рынка: мин/макс/средняя/медианная цена, перцентили, топ предложений
- Графики: распределение цен, цены по городам, динамика по истории сканирований
- Email-отчёт с встроенными графиками
- Планировщик с интервалом 3 дня (настраивается)
- Поддержка прокси для доступа с российского IP

## Быстрый старт

```bash
# 1. Установка зависимостей
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1.1. Браузер для Авито (обязательно для реального сканирования)
playwright install chromium

# 2. Настройка
cp .env.example .env
# Отредактируйте .env — укажите SMTP и email получателя

# 3. Тестовый запуск (демо-данные, без email)
python -m avito_monitor scan --demo --no-email

# 4. Реальное сканирование
python -m avito_monitor scan --no-email

# 5. Запуск планировщика (сканирование + email каждые 3 дня)
python -m avito_monitor schedule
```

## Настройка

Параметры задаются в `config.yaml` и/или `.env`:

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `PRODUCT` | Скамейки | Поисковый запрос |
| `REGION` | Дагестан | Название региона |
| `REGION_SLUG` | dagestan | Slug региона в URL Авито |
| `LOCATION_ID` | 646710 | ID региона в API Авито |
| `REPORT_INTERVAL_DAYS` | 3 | Интервал отправки отчётов |
| `SMTP_*` | — | Настройки почтового сервера |
| `PROXY` | — | HTTP/SOCKS прокси (рекомендуется) |
| `USE_BROWSER` | true | Сканирование через Playwright (как в браузере) |
| `RATE_LIMIT_BACKOFF_SECONDS` | 15 | Пауза при ошибке 429 от Авито |

Определить `location_id` для другого региона:

```bash
python -m avito_monitor resolve-region
```

## Команды

```bash
python -m avito_monitor scan              # одно сканирование + отчёт + email
python -m avito_monitor scan --no-email   # без отправки на почту
python -m avito_monitor scan --demo       # демо-данные
python -m avito_monitor schedule          # фоновый планировщик
python -m avito_monitor show-config       # текущие настройки
```

## Структура отчёта

- Ключевые показатели (мин, макс, средняя, медиана)
- Перцентили цен (P10–P90)
- Топ-5 самых дешёвых и дорогих предложений со ссылками
- Графики:
  - распределение цен
  - средняя цена по городам
  - количество объявлений по городам
  - разброс цен (boxplot)
  - динамика по истории сканирований
- Автоматические выводы по рынку

Отчёты сохраняются в папку `reports/`.

## Важно про доступ к Авито

Авито ограничивает частые HTTP-запросы (ошибка 429) и подгружает объявления через JavaScript.

**Рекомендуется браузерный режим (Playwright):**

```bash
pip install playwright
playwright install chromium
```

В `.env` должно быть `USE_BROWSER=true` (включено по умолчанию).

Дополнительно:

1. Запускайте программу на сервере в России, **или**
2. Укажите российский прокси в `PROXY=http://user:pass@host:port`

При блокировке программа автоматически формирует отчёт на демо-данных (с пометкой в отчёте), чтобы вы могли проверить работу email и графиков.

## Docker (опционально)

```bash
docker build -t avito-monitor .
docker run -d --env-file .env -v $(pwd)/data:/app/data -v $(pwd)/reports:/app/reports avito-monitor
```

## Настройка Gmail

1. Включите двухфакторную аутентификацию
2. Создайте [пароль приложения](https://myaccount.google.com/apppasswords)
3. Укажите в `.env`:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USERNAME=your@gmail.com
   SMTP_PASSWORD=xxxx xxxx xxxx xxxx
   SMTP_FROM=your@gmail.com
   SMTP_TO=recipient@example.com
   ```
