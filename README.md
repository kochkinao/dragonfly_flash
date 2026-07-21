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
# Optional: linked discussion group for future comment mirroring.
TELEGRAM_DISCUSSION_CHAT_ID=-100xxxxxxxxxx
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

## Мониторинг лайков/комментариев

Скрипт умеет обновлять уже опубликованные Telegram-посты статистикой Dragonfly:

```text
❤️ 12   💬 4
```

Как это работает:

- при отправке нового поста сохраняется mapping `Dragonfly post_id -> Telegram message_id`;
- сохраняется базовый HTML первого сообщения/первой caption;
- `sync-stats` берёт последние N постов из `/api/feed`, читает `likes_count` и `comments_count`;
- если счётчики изменились — редактирует Telegram-сообщение через `editMessageText` или `editMessageCaption`;
- старые посты без сохранённого `message_id` пропускаются.

Один проход:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  sync-stats --count 20
```

Постоянный мониторинг раз в минуту:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  sync-stats-watch --count 20 --interval 60
```

Для read-only распределения нагрузки можно закрепить watcher за отдельным аккаунтом из `DRAGONFLY_ACCOUNTS_FILE`, не меняя глобальный `active`:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  --dragonfly-account backup_1 \
  sync-stats-watch --count 50 --interval 30
```

## Telegram discussion group

Если канал привязан к группе обсуждений, укажите её ID:

```env
TELEGRAM_DISCUSSION_CHAT_ID=-100xxxxxxxxxx
```

После отправки нового поста скрипт пробует через `getUpdates` найти автоматический forward Telegram в discussion group и сохраняет mapping:

```text
Dragonfly post_id + role=last -> discussion_message_id
```

Это подготовка для зеркалирования комментариев: если Dragonfly-пост разбит на несколько Telegram-сообщений, комментарии нужно отправлять reply именно к `role=last`.

## Зеркалирование комментариев Dragonfly

Комментарии берутся из endpoint:

```text
/api/get_comments/<post_id>?user_id=<DRAGONFLY_USER_ID>
```

Первый проход по посту по умолчанию только помечает уже существующие комментарии как увиденные, чтобы не заспамить чат старыми комментариями. Новые комментарии после этого отправляются в discussion group ответом на `role=last`.

Один проход:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  sync-comments --count 20
```

Постоянный watcher:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  sync-comments-watch --count 20 --interval 30
```

Для отдельного read-only аккаунта и расширенного окна:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  --dragonfly-account backup_2 \
  sync-comments-watch --count 50 --interval 30 --send-existing
```

Если нужно отправить уже существующие комментарии тоже, добавьте `--send-existing`.

## Загрузка треков в Dragonfly

Для массовой загрузки аудио есть отдельный скрипт:

```bash
python3 dragonfly_audio_uploader.py \
  --env-file /home/wacotal/dragonfly.env \
  --delay 5 \
  --jitter 1 \
  /path/to/music
```

Что делает:

- отправляет `POST /api/audio/upload`;
- multipart-поля как в браузере: `artist`, `title`, `file`;
- берёт авторизацию из `DRAGONFLY_ACCOUNTS_FILE` / `DRAGONFLY_ACCESS_TOKEN`;
- при `401` переключает аккаунт из пула;
- при `429` ждёт backoff и повторяет;
- поддерживает файлы, glob и директории;
- по умолчанию понимает имена вида `Artist - Title.mp3`;
- лимит размера по умолчанию: 25 MB.

Проверить без загрузки:

```bash
python3 dragonfly_audio_uploader.py \
  --env-file /home/wacotal/dragonfly.env \
  --dry-run \
  /path/to/music
```

Один файл с ручным artist/title:

```bash
python3 dragonfly_audio_uploader.py \
  --env-file /home/wacotal/dragonfly.env \
  --artist "Artist" \
  --title "Track title" \
  ./track.mp3
```
