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
- Логирует в stdout и файл `~/.hermes/logs/dragonfly_telegram_poster.log`; файл ротируется `10 MB × 5`.
- Дополнительно отправляет user-friendly предупреждения в личку (`TELEGRAM_ALERT_CHAT_ID`): auth/permanent errors и важные failures; transient network/429 retry events остаются в логах без спама в Telegram.
- При Telegram 429 ждёт `retry_after + 2` секунд и продолжает.
- При Dragonfly 429 использует adaptive backoff: 30 сек → 1 мин → 2 мин → 4 мин → ... до 15 мин, чтобы временный rate limit не ронял процесс.
- Telegram pacing гибкий по типам постов: `--text-delay`, `--photo-delay`, `--album-delay`, `--animation-delay`, `--mixed-media-delay`, `--media-item-delay`.

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
TELEGRAM_ADMIN_USER_ID=123456789
TELEGRAM_DISCUSSION_CHAT_ID=-100xxxxxxxxxx
TELEGRAM_BEST_CHAT_ID=-100xxxxxxxxxx
DRAGONFLY_USER_ID=2723
DRAGONFLY_ACCOUNTS_FILE=/path/to/dragonfly_accounts.json
```

`DRAGONFLY_ACCESS_TOKEN` — legacy JWT fallback. Текущий сайт Dragonfly уже использует HttpOnly-cookie сессии, поэтому надёжнее перейти на `DRAGONFLY_COOKIE_FILE` или `DRAGONFLY_ACCOUNTS_FILE`.

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

или:

```text
auth-check OK mode=accounts:main sample_posts=20
```

Если Dragonfly вернёт `401`, скрипт отправит понятный alert в личку: авторизация истекла, нужно обновить cookie/JWT.

### Несколько аккаунтов для failover/read-only watchers

Можно указать JSON-файл с несколькими Dragonfly `access_token`:

```bash
cp dragonfly_accounts.example.json /secure/path/dragonfly_accounts.json
chmod 600 /secure/path/dragonfly_accounts.json
```

Подключение:

```bash
DRAGONFLY_ACCOUNTS_FILE=/secure/path/dragonfly_accounts.json
```

В multi-account режиме скрипт использует активный аккаунт из JSON. Если Dragonfly вернул `401`, скрипт переключает `active` на следующий enabled-аккаунт, отправляет alert в личку и повторяет запрос. При `429` аккаунты не переключаются: используется adaptive backoff, чтобы не забанить весь пул.

Для приватного канала вместо `@dragonfly_flash` нужен numeric chat_id. Бот должен быть добавлен в канал админом с правом публикации; для comments mirroring бот также должен быть в linked discussion group.

### Telegram admin commands

Если задан `TELEGRAM_ADMIN_USER_ID`, отдельный процесс `dragonfly-admin-bot` принимает admin-команды в личке с тем же ботом, который публикует посты и комментарии. Доступ разрешён только Telegram `from.id`, равному `TELEGRAM_ADMIN_USER_ID`; остальные получают короткое `Нет доступа`. Секреты/env/token values команды не показывают.

Команды:

```text
/panel
/menu
/start
/help
/status
/get request_delay
/set request_delay 2
```

Основной интерфейс — `/panel`: бот показывает список параметров. Нажмите конкретный параметр, и откроется отдельный экран с:

- объяснением, за что отвечает параметр;
- текущим значением и пометкой `(default)`, если значение не переопределено;
- кнопками готовых значений;
- кнопкой `✍️ Ввести своё` для ручного ввода любого числа секунд;
- кнопкой `Default` для сброса только этого параметра;
- кнопкой `↩️ Назад к параметрам`.

На главном экране есть кнопка `↩️ Сбросить всё к default`, которая удаляет все runtime override'ы и возвращает production-дефолты.

Доступные настройки в панели:

```text
request_delay
send_delay
text_delay
photo_delay
album_delay
media_item_delay
poll_interval
feed_cache_interval
stats_hot_interval
stats_cold_interval
comments_interval
```

Ручные `/set` и `/get` оставлены как fallback, если кнопки недоступны.

Настройки сохраняются в SQLite (`kv` keys `runtime_setting.*`) и применяются watcher'ами на следующем цикле без ручного рестарта. Telegram `getUpdates` long-polling выполняет только `dragonfly-admin-bot`; остальные процессы используют общий SQLite update cache, поэтому не запускайте второй независимый long-poller с тем же bot token.

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
  --photo-delay 5 \
  --album-delay 5 \
  --animation-delay 5 \
  --mixed-media-delay 5 \
  --media-item-delay 2 \
  --poll-interval 15 \
  --max-attempts 3 \
  --keep-sent 50000 \
  watch --max-gap-scan 200
```

Если gap между видимыми post id стабильно больше `--max-gap-scan`, warning `gap catch-up skipped` логируется один раз для этого диапазона/лимита, а не на каждом 15-секундном цикле.

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

## База состояния и логи

По умолчанию:

```text
~/.hermes/state/dragonfly_telegram_poster.sqlite3
```

Лог по умолчанию:

```text
~/.hermes/logs/dragonfly_telegram_poster.log
```

Лог ротируется автоматически: `10 MB × 5` backup-файлов.

Можно указать другую базу:

```bash
python3 dragonfly_telegram_poster.py \
  --db /path/to/state.sqlite3 \
  backfill --count 1000
```

## Канал «Лучшее»

Посты, набравшие минимум 7 лайков, можно пересылать в отдельный канал:

```bash
TELEGRAM_BEST_CHAT_ID=-100xxxxxxxxxx
```

В текущей production-схеме отдельный `sync-best-watch` не нужен: `sync-stats` уже читает feed counters и сам пересылает qualifying posts в best channel с SQLite dedupe. Standalone `sync-best`/`sync-best-watch` оставлены для ручного backfill или изоляции.

## Stats footer

Для уже опубликованных постов можно обновлять footer:

```text
❤️ N   💬 M
```

Один проход:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  sync-stats --count 20
```

Постоянные watcher'ы через общий feed cache:

```bash
# one Dragonfly feed reader for offsets 0–59
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  --dragonfly-account backup_1 \
  --request-delay 2 \
  refresh-feed-cache-watch --count 60 --offset 0 --interval 20

# last 20 — every 30s, local SQLite feed cache
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  --dragonfly-account backup_1 \
  --request-delay 2 \
  sync-stats-watch --count 20 --offset 0 --interval 30 --use-feed-cache

# posts 21–50 — every 60s, local SQLite feed cache
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  --dragonfly-account backup_1 \
  --request-delay 2 \
  sync-stats-watch --count 30 --offset 20 --interval 60 --use-feed-cache
```

`refresh-feed-cache-watch` один ходит в `/api/feed` и складывает окно постов в SQLite `cached_feed_posts`. Stats/best watcher'ы с `--use-feed-cache` читают это окно локально и не создают отдельные feed-запросы.

Stats редактируются только на `role=main`. Для `💬` используется максимум из feed `comments_count` и числа уже зеркалированных Telegram comments, чтобы Telegram footer не показывал меньше, чем реально отправлено в discussion.

## Discussion group / comments

После отправки нового поста скрипт пробует через `getUpdates` найти автоматический forward Telegram в discussion group и сохраняет mapping:

```text
Dragonfly post_id + role=last -> discussion_message_id
```

Если mapping не был пойман сразу, можно повторно пройти последние отсутствующие записи:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  repair-discussion-mapping --count 200 --wait-seconds 0 --update-timeout 0
```

Команда ищет `telegram_messages.role='last'` без строки в `telegram_discussion_messages`, один раз читает доступный `getUpdates` snapshot и сопоставляет все missing rows внутри него. Старые missing mappings могут быть уже недоступны, если Telegram updates offset ушёл вперёд.

## Зеркалирование комментариев Dragonfly

Endpoint:

```text
/api/get_comments/<post_id>?user_id=<DRAGONFLY_USER_ID>
```

Первый проход по посту по умолчанию только помечает уже существующие комментарии как увиденные, чтобы не заспамить чат старыми комментариями. Новые комментарии после этого отправляются в discussion group ответом на `role=last`.

Постоянная production-схема на 50 постов через общий feed cache:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  --dragonfly-account backup_2 \
  sync-comments-watch --count 17 --offset 0 --interval 30 --send-existing --hot-count 20 --use-feed-cache

python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  --dragonfly-account backup_3 \
  sync-comments-watch --count 17 --offset 17 --interval 30 --send-existing --hot-count 20 --use-feed-cache

python3 dragonfly_telegram_poster.py \
  --env-file /home/wacotal/dragonfly.env \
  --dragonfly-account backup_4 \
  sync-comments-watch --count 16 --offset 34 --interval 30 --send-existing --hot-count 20 --use-feed-cache
```

Comments watcher использует gating: для постов вне `--hot-count` он не ходит в `/api/get_comments/<post_id>`, если feed `comments_count` не вырос и нет ранее seeded комментариев без Telegram `message_id`. Перед отправкой каждый comment атомарно резервируется в SQLite, поэтому при overlap/shard-сдвигах только один процесс имеет право отправить конкретный `(post_id, comment_id)`. Если новый комментарий отправлен, watcher сразу обновляет stats footer этого поста, чтобы `💬` совпадал с discussion.

## Production systemd / перенос на другой сервер

В репозитории есть переносимые шаблоны user-systemd unit'ов:

```text
deploy/systemd/user/dragonfly-bridge.target
deploy/systemd/user/dragonfly-feed-cache.service
deploy/systemd/user/dragonfly-watch.service
deploy/systemd/user/dragonfly-stats-hot.service
deploy/systemd/user/dragonfly-stats-cold.service
deploy/systemd/user/dragonfly-comments-0.service
deploy/systemd/user/dragonfly-comments-17.service
deploy/systemd/user/dragonfly-comments-34.service
```

Они не содержат локальных путей. На сервере их рендерит installer:

```bash
python3 scripts/install_systemd_user.py \
  --project-dir "$PWD" \
  --env-file "$HOME/dragonfly.env"
```

Проверить, что unit'ы появились:

```bash
systemctl --user daemon-reload
systemctl --user list-unit-files 'dragonfly-*'
```

Запустить весь bridge:

```bash
systemctl --user start dragonfly-bridge.target
systemctl --user status dragonfly-bridge.target
```

Включить автозапуск при user login:

```bash
systemctl --user enable dragonfly-bridge.target
```

Чтобы user services переживали logout/reboot, на сервере обычно нужно один раз от root:

```bash
sudo loginctl enable-linger "$USER"
```

### Минимальный migration checklist

```bash
git clone git@github.com:kochkinao/dragonfly_flash.git
cd dragonfly_flash
python3 -m py_compile dragonfly_telegram_poster.py
cp dragonfly.env.example ~/dragonfly.env
cp dragonfly_accounts.example.json ~/.dragonfly_accounts.json
chmod 600 ~/dragonfly.env ~/.dragonfly_accounts.json
# заполнить реальные Telegram/Dragonfly значения в ~/dragonfly.env и ~/.dragonfly_accounts.json
python3 dragonfly_telegram_poster.py --env-file ~/dragonfly.env doctor --no-network
python3 dragonfly_telegram_poster.py --env-file ~/dragonfly.env doctor
python3 dragonfly_telegram_poster.py --env-file ~/dragonfly.env auth-check
python3 scripts/install_systemd_user.py --project-dir "$PWD" --env-file ~/dragonfly.env --enable --start
```

`doctor --no-network` проверяет локальные prerequisites: env/config, account pool, writable SQLite/log paths, systemd templates. `doctor` дополнительно проверяет Telegram `getMe`/`getChat` и доступность Dragonfly feed. Команда не печатает токены/cookie.

### Backup/restore runtime state

Перед переносом сервера создайте архив состояния на старом сервере:

```bash
python3 dragonfly_telegram_poster.py \
  --env-file ~/dragonfly.env \
  export-state --output ~/dragonfly-state-$(date +%Y%m%d-%H%M%S).tar.gz
```

Архив содержит:

```text
manifest.json
state/dragonfly_telegram_poster.sqlite3
secrets/dragonfly_accounts.json, если DRAGONFLY_ACCOUNTS_FILE задан
```

Архив намеренно не содержит `.env`, cookie jar, логи, SSH keys или PM2/systemd runtime files. Сам архив создаётся с правами `0600`, потому что account pool внутри него содержит реальные Dragonfly access tokens.

На новом сервере восстановите состояние после clone и настройки `~/dragonfly.env`:

```bash
python3 dragonfly_telegram_poster.py \
  import-state ~/dragonfly-state-YYYYMMDD-HHMMSS.tar.gz \
  --db ~/.hermes/state/dragonfly_telegram_poster.sqlite3 \
  --accounts-file ~/.dragonfly_accounts.json
```

После restore проверьте:

```bash
python3 dragonfly_telegram_poster.py --env-file ~/dragonfly.env doctor
```

Не переносите в Git реальные `.env`, cookie jar, SQLite state, логи, токены, SSH keys или `Domain name.txt`.

## Production PM2 / prod-branch автодеплой

Для нового production-сервера предпочтительная схема — PM2 + `prod` branch:

```text
local dev -> tests -> git push origin main/prod
server -> watches origin/prod -> git reset --hard origin/prod -> doctor --no-network -> pm2 reload
```

PM2 process file:

```text
ecosystem.config.cjs
```

Он запускает 8 процессов:

```text
dragonfly-admin-bot       Telegram commands + update cache
dragonfly-watch
dragonfly-feed-cache
dragonfly-stats-hot       --use-feed-cache
dragonfly-stats-cold      --use-feed-cache
dragonfly-comments-0      --use-feed-cache
dragonfly-comments-17     --use-feed-cache
dragonfly-comments-34     --use-feed-cache
```

### Первичная настройка PM2 на сервере

```bash
# Node/PM2
npm install -g pm2

# repo
mkdir -p ~/apps
cd ~/apps
git clone git@github.com:kochkinao/dragonfly_flash.git
cd dragonfly_flash
git checkout prod

# secrets/state: НЕ из Git и НЕ в ~/.hermes другого Hermes
mkdir -p ~/dragonfly/state ~/dragonfly/logs
cp dragonfly.env.example ~/dragonfly/dragonfly.env
chmod 600 ~/dragonfly/dragonfly.env
# заполнить ~/dragonfly/dragonfly.env реальными значениями

# если переносим существующий runtime state:
python3 dragonfly_telegram_poster.py \
  import-state ~/dragonfly-state-YYYYMMDD-HHMMSS.tar.gz \
  --db ~/dragonfly/state/dragonfly_telegram_poster.sqlite3 \
  --accounts-file ~/dragonfly/state/dragonfly_accounts.json

python3 dragonfly_telegram_poster.py --env-file ~/dragonfly/dragonfly.env --db ~/dragonfly/state/dragonfly_telegram_poster.sqlite3 --log-file ~/dragonfly/logs/dragonfly_telegram_poster.log doctor --no-network
python3 dragonfly_telegram_poster.py --env-file ~/dragonfly/dragonfly.env --db ~/dragonfly/state/dragonfly_telegram_poster.sqlite3 --log-file ~/dragonfly/logs/dragonfly_telegram_poster.log doctor

DRAGONFLY_ENV_FILE=~/dragonfly/dragonfly.env DRAGONFLY_STATE_DIR=~/dragonfly/state DRAGONFLY_LOG_DIR=~/dragonfly/logs pm2 startOrReload ecosystem.config.cjs --update-env
pm2 save
pm2 startup
```

`pm2 startup` напечатает команду с `sudo`; её нужно выполнить один раз, чтобы PM2 воскресал после reboot.

Проверка:

```bash
pm2 status
scripts/pm2_status.sh
```

### Ручной deploy с prod branch

```bash
cd ~/apps/dragonfly_flash
DRAGONFLY_ENV_FILE=~/dragonfly/dragonfly.env DRAGONFLY_STATE_DIR=~/dragonfly/state DRAGONFLY_LOG_DIR=~/dragonfly/logs scripts/deploy_prod.sh
```

`deploy_prod.sh` делает:

```text
git fetch origin prod
git reset --hard origin/prod
python3 -m py_compile ...
python3 dragonfly_telegram_poster.py --env-file "$DRAGONFLY_ENV_FILE" --db "$DRAGONFLY_STATE_DIR/dragonfly_telegram_poster.sqlite3" doctor --no-network
pm2 startOrReload ecosystem.config.cjs --update-env
pm2 save
```

### Автодеплой через poller

Самый простой безопасный вариант — poller на сервере без публичного webhook endpoint:

```bash
cd ~/apps/dragonfly_flash
PM2_DEPLOY_INTERVAL=60 \
DRAGONFLY_ENV_FILE=~/dragonfly/dragonfly.env \
DRAGONFLY_STATE_DIR=~/dragonfly/state \
DRAGONFLY_LOG_DIR=~/dragonfly/logs \
  pm2 start scripts/prod_branch_poller.sh --name dragonfly-prod-poller --interpreter bash
pm2 save
```

Теперь сервер будет проверять `origin/prod` раз в минуту и перезапускать bridge только если SHA изменился.

Переменные:

```text
DRAGONFLY_ENV_FILE      default: ~/dragonfly/dragonfly.env
DRAGONFLY_STATE_DIR     default: ~/dragonfly/state
DRAGONFLY_LOG_DIR       default: ~/dragonfly/logs
PM2_DEPLOY_REMOTE       default: origin
PM2_DEPLOY_BRANCH       default: prod
PM2_DEPLOY_INTERVAL     default: 60
PYTHON                  default: python3
```

Остановить автодеплой:

```bash
pm2 stop dragonfly-prod-poller
pm2 delete dragonfly-prod-poller
pm2 save
```

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
  --title "Title" \
  ./track.mp3
```
