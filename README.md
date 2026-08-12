# Ficbook Telegram Bot

Telegram-бот на `aiogram`, который принимает ссылки на поддерживаемые литературные сайты, загружает текст, собирает выбранные пользователем форматы и отправляет файлы в ответ.

## Возможности

- принимает ссылки Ficbook, AO3, Wattpad, Hogwartsnet и Litnet
- скачивает бесплатные книги Litnet и сразу сообщает, если книга платная
- скачивает многоглавные фанфики по главам
- умеет скачивать только выбранные главы в формате `1,2,5-10,17`
- добавляет обложку в `fb2`, `epub`, `docx` и `pdf`, если она есть у фанфика
- собирает итоговые `fb2`, `epub`, `txt`, `docx` и `pdf` в памяти
- добавляет реальные номера страниц в `docx` и `pdf`
- сохраняет в DOCX жирный/курсив, цитаты, списки, оглавление, метаданные и примечания
- добавляет в EPUB оглавление, расширенные метаданные и оформление примечаний
- сохраняет пейринги и интерактивные примечания Ficbook
- позволяет выбрать форматы и включить выбор отдельных глав через `/settings`
- показывает команды в меню Telegram рядом с полем ввода сообщения
- позволяет включать или выключать добавление обложки через `/settings`
- позволяет написать администратору через `/help`
- использует отдельные очереди для сайтов и каждого Ficbook-аккаунта
- показывает позицию, примерное ожидание и прогресс скачивания
- сохраняет базовое форматирование EPUB: главы, абзацы, списки, цитаты, жирный и курсивный текст
- пишет аналитику пользователей и скачиваний в PostgreSQL
- отдаёт Prometheus-метрики на `/metrics` для Grafana/Prometheus
- поддерживает авторизацию для `18+` через переменные логина и пароля нужного сайта
- распределяет Ficbook-задания циклически между пятью аккаунтами
- переносит задание на следующий Ficbook-аккаунт при `429` или ошибке входа
- готов для запуска в Docker

## Запуск

```bash
cp .env.example .env
docker compose up --build -d
```

## Переменные окружения

- `BOT_TOKEN` — токен Telegram-бота
- `FICBOOK_LOGIN` — логин Ficbook для `18+`
- `FICBOOK_PASSWORD` — пароль Ficbook для `18+`
- `FICBOOK_BACKUP_LOGIN` — запасной логин Ficbook для повторной попытки при `429`
- `FICBOOK_BACKUP_PASSWORD` — пароль запасного Ficbook-аккаунта
- `FICBOOK_ACCOUNT_3_LOGIN`, `FICBOOK_ACCOUNT_3_PASSWORD` — третий Ficbook-аккаунт
- `FICBOOK_ACCOUNT_4_LOGIN`, `FICBOOK_ACCOUNT_4_PASSWORD` — четвёртый Ficbook-аккаунт
- `FICBOOK_ACCOUNT_5_LOGIN`, `FICBOOK_ACCOUNT_5_PASSWORD` — пятый Ficbook-аккаунт
- `AO3_LOGIN`, `AO3_PASSWORD` — необязательный аккаунт AO3 для доступных ему произведений
- `WATTPAD_LOGIN`, `WATTPAD_PASSWORD` — логин и пароль Wattpad, если работа требует вход
- `HOGWARTSNET_LOGIN`, `HOGWARTSNET_PASSWORD` — логин и пароль Hogwartsnet для закрытого доступа
- `LITNET_LOGIN`, `LITNET_PASSWORD` — необязательные данные Litnet; бесплатные книги работают без входа
- `FICBOOK_MAX_CONCURRENT_DOWNLOADS` — устаревшая совместимая настройка; параллельность определяется аккаунтами
- `FICBOOK_DOWNLOAD_INTERVAL_SECONDS` — минимальная пауза между стартами скачиваний, по умолчанию `8`
- `FICBOOK_REQUEST_DELAY_SECONDS` — пауза между запросами к Ficbook внутри одного скачивания, по умолчанию `1.5`
- `FICBOOK_RETRY_ATTEMPTS` — число попыток на аккаунт при `429`, по умолчанию `3`
- `FICBOOK_RETRY_BASE_DELAY_SECONDS` — первая пауза backoff при `429`, по умолчанию `8`
- `FICBOOK_RETRY_MAX_DELAY_SECONDS` — максимальная пауза backoff при `429`, по умолчанию `45`
- `ADMIN_CHAT_ID` — ваш `chat_id` для уведомлений об ошибках пользователей
- `ALERT_BOT_TOKEN` — отдельный бот для админских уведомлений, если нужен
- `PREMIUM_QUEUE_USER_IDS` — user id через запятую, которые проходят очередь первыми
- `LOG_LEVEL` — уровень логирования, по умолчанию `INFO`
- `DATABASE_URL` — PostgreSQL DSN для аналитики
- `METRICS_PORT` — внутренний порт HTTP endpoint `/metrics`, по умолчанию `8000`
- `METRICS_HOST_PORT` — порт на хосте для `/metrics`, по умолчанию `18000`
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` — параметры PostgreSQL, если база запускается отдельно
- `POSTGRES_HOST_PORT` — локальный порт PostgreSQL для Grafana datasource, по умолчанию `15432`

## Поведение

Бот отвечает на сообщение со ссылкой на фанфик и отправляет выбранные пользователем форматы. По умолчанию включены `.fb2`, `.epub`, `.txt`, `.docx` и `.pdf`; изменить выбор можно командой `/settings`. В настройках есть отдельные разделы для форматов файлов и обложки. Там же можно включить выбор отдельных глав: после ссылки бот спросит номера глав, а пользователь отвечает, например, `1,2,5-10,17` или `0` для всех глав. Если фик `18+`, но логин или пароль не заданы, бот вернет понятную текстовую ошибку вместо технического сообщения. Если задан `ADMIN_CHAT_ID`, бот дополнительно пришлет вам сообщение с данными пользователя и текстом ошибки.

Для каждого сайта используется отдельная очередь, а для каждого заполненного Ficbook-аккаунта создаётся собственная однопоточная очередь. Ficbook-ссылки распределяются циклически по аккаунтам. Ожидающий пользователь видит только своё место и пересчитываемое время, а активная загрузка обновляет процент не чаще одного раза в 5 секунд. Оценка ожидания использует две минуты на один фанфик. Команда `/queue` доступна только администратору и показывает суммарное число активных и ожидающих фанфиков.

Бот не обходит CAPTCHA, оплату или ограничения доступа. Публичные произведения AO3 и бесплатные книги Litnet скачиваются без входа. Если Ficbook показывает страницу технических работ, активные задания остаются в очередях и автоматически продолжаются после восстановления сайта.

## Структура проекта

```text
src/
├── bot/          # Telegram handlers, keyboards and session types
├── core/         # configuration, models, chapter parser and queues
├── sources/      # Ficbook, Litnet and source URL adapters
├── exporters/    # FB2, EPUB, TXT, DOCX and PDF generation
├── storage/      # PostgreSQL analytics and user settings
├── monitoring/   # Prometheus metrics endpoint
└── main.py       # application entry point
```

Секреты хранятся только в локальном `.env`. В Git добавляется `.env.example` с пустыми или демонстрационными значениями.

## Аналитика

Данные хранятся в PostgreSQL:

- `bot_users` — пользователи Telegram, `/start`, username, full name, first/last seen.
- `downloads` — попытки скачивания, статус `attempt/success/error`, URL, название фанфика, ошибка.
- `user_format_settings` — выбранные пользователями форматы, обложка и режим выбора глав.

Готовые view для Grafana PostgreSQL datasource:

- `analytics_summary` — всего пользователей `/start`, активные за 30 дней, попытки, успешные скачивания, success ratio.
- `analytics_users` — username, full name, количество скачанных фанфиков, последняя ссылка.
- `analytics_top_fanfics` — топ-5 фанфиков по успешным скачиваниям.

Подключение Grafana к PostgreSQL с хоста:

- Host: `localhost:15432`
- Database: `ficbook_bot`
- User: `ficbook`
- Password: значение `POSTGRES_PASSWORD`

Prometheus endpoint:

- `http://localhost:18000/metrics`

Основные метрики:

- `ficbook_bot_started_users_total`
- `ficbook_bot_active_users_30d`
- `ficbook_bot_download_attempts_total`
- `ficbook_bot_successful_downloads_total`
- `ficbook_bot_download_success_ratio`
- `ficbook_bot_user_downloads_total`
- `ficbook_bot_top_fanfic_downloads`
