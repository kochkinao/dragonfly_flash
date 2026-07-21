# Dragonfly Flash -> Telegram channel

Скрипт: `dragonfly_telegram_poster.py`

Канал по умолчанию: `@dragonfly_flash` (`https://t.me/dragonfly_flash`).

## Что делает

- Парсит `https://dragonfly-flash.ru/api/feed?type=all&limit=20&offset=0`.
- Отправляет в Telegram текст и картинки/GIF.
- Если к посту приложена музыка, добавляет секцию `🎵` с artist/title/duration/filename, чтобы трек можно было найти вручную.
- Аудио-файлы не отправляет.
- Пустые посты без текста, фото/GIF и информации о музыке не отправляет, но помечает как обработанные.
- Медиа отправляет через upload: временно скачивает bytes в память процесса и загружает в Telegram multipart, потому что Telegram не всегда может сам скачать URL Dragonfly.
- Не дублирует посты: состояние хранится в SQLite.
- Навёрстывает пропуски по `post_id`: после feed-пачки сканирует дыры в диапазоне ID через `/api/post/<id>` и отправляет найденные посты.
- Длинный текст, служебные подписи, ссылка на пост и секция музыки считаются вместе внутри лимитов Telegram и режутся на несколько сообщений/caption.
- Части подписывает в стиле Telegram: `(1/2)`, `(2/2)`.
- Больше 10 фото отправляет несколькими альбомами.
- GIF отправляет через `sendAnimation`.
- Если медиа не отправилось, публикует fallback: текст + предупреждение + ссылка на оригинал.
- Если пост всё равно падает, retry до `--max-attempts` раз, потом пропуск, чтобы не блокировать очередь.
- Автоочистка SQLite: хранит только последние `--keep-sent` успешных записей, failed-записи сохраняет.
- Логирует в stdout и файл `~/.hermes/logs/dragonfly_telegram_poster.log`; ошибки пишутся с traceback.
- Дополнительно отправляет user-friendly предупреждения в личку (`TELEGRAM_ALERT_CHAT_ID`): 429, retry/wait, временные ошибки постов, permanent fail, watch-loop errors.
- При Telegram 429 ждёт `retry_after + 2` секунд и продолжает.
- При Dragonfly 429 использует adaptive backoff: 30 сек → 1 мин → 2 мин → 4 мин → ... до 15 мин, чтобы временный rate limit не ронял процесс.
- Telegram pacing гибкий по типам постов: `--text-delay`, `--photo-delay`, `--album-delay`, `--animation-delay`, `--mixed-media-delay`, `--media-item-delay`. Поэтому текст/обычные фото не тормозятся так же жёстко, как GIF/animation.

## .env

Шаблон:

```bash
cp dragonfly.env.example dragonfly.env
chmod 600 dragonfly.env
```

Формат:

```bash
DRAGONFLY_ACCESS_TOKEN=...
# Preferred long-term auth. If set, cookie auth is used instead of legacy JWT.
DRAGONFLY_COOKIE_FILE=/path/to/dragonfly_cookies.txt
TELEGRAM_BOT_TOKEN=***
TELEGRAM_CHAT_ID=@dragonfly_flash
TELEGRAM_ALERT_CHAT_ID=123456789
# Optional: account pool for 401/auth failover.
DRAGONFLY_ACCOUNTS_FILE=/path/to/dragonfly_accounts.json
```

`DRAGONFLY_ACCESS_TOKEN` — legacy JWT fallback. Текущий сайт Dragonfly уже использует HttpOnly-cookie сессии, поэтому надёжнее перейти на `DRAGONFLY_COOKIE_FILE`.

Проверка авторизации:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file dragonfly.env \
  auth-check
```

Ожидаемый результат:

```text
auth-check OK mode=cookie sample_posts=20
```

или, пока используется JWT fallback:

```text
auth-check OK mode=bearer-jwt sample_posts=20
```

Если Dragonfly вернёт `401`, скрипт отправит понятный alert в личку: авторизация истекла, нужно обновить cookie/JWT.

### Несколько аккаунтов для failover

Можно указать JSON-файл с несколькими Dragonfly `access_token`:

```json
{
  "active": "main",
  "accounts": [
    {"name": "main", "access_token": "...", "enabled": true},
    {"name": "backup_1", "access_token": "...", "enabled": true}
  ]
}
```

Подключение:

```bash
DRAGONFLY_ACCOUNTS_FILE=/path/to/dragonfly_accounts.json
```

В multi-account режиме скрипт использует активный аккаунт из JSON. Если Dragonfly вернул `401`, скрипт переключает `active` на следующий enabled-аккаунт, отправляет alert в личку и повторяет запрос. При `429` аккаунты не переключаются: используется adaptive backoff, чтобы не забанить весь пул.

Для приватного канала вместо `@dragonfly_flash` нужен numeric chat_id.
Бот должен быть добавлен в канал админом с правом публикации.

## Рекомендуемые безопасные параметры

Backfill последних 1000 постов:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file dragonfly.env \
  --request-delay 8 \
  --send-delay 2 \
  --text-delay 2 \
  --photo-delay 6 \
  --album-delay 12 \
  --animation-delay 45 \
  --mixed-media-delay 45 \
  --media-item-delay 12 \
  --max-attempts 3 \
  --keep-sent 50000 \
  backfill --count 1000 --max-gap-scan 5000
```

Watch mode каждые 15 секунд:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file dragonfly.env \
  --request-delay 2 \
  --send-delay 2 \
  --text-delay 2 \
  --photo-delay 6 \
  --album-delay 12 \
  --animation-delay 45 \
  --mixed-media-delay 45 \
  --media-item-delay 12 \
  --poll-interval 15 \
  --max-attempts 3 \
  --keep-sent 50000 \
  watch --max-gap-scan 200
```

## Не отправлять старое, начать только с новых

```bash
python3 dragonfly_telegram_poster.py \
  --env-file dragonfly.env \
  init --count 1000

python3 dragonfly_telegram_poster.py \
  --env-file dragonfly.env \
  --poll-interval 15 \
  --keep-sent 50000 \
  watch
```

## Dry-run без Telegram

```bash
DRAGONFLY_ACCESS_TOKEN='...' \
python3 dragonfly_telegram_poster.py --dry-run backfill --count 20
```

## База состояния

По умолчанию:

```text
~/.hermes/state/dragonfly_telegram_poster.sqlite3
```

Можно указать другую:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file dragonfly.env \
  --db ./dragonfly.sqlite3 \
  watch
```

## Тесты

```bash
python3 test_dragonfly_telegram_poster.py
```
