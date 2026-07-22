#!/usr/bin/env python3
"""Dragonfly Flash -> Telegram channel poster.

Features:
- text + images only; audio is intentionally ignored
- no local media storage: sends image URLs to Telegram
- SQLite state to avoid duplicate posts
- backfill last N posts with conservative delays
- watch mode polling every 15 seconds by default

Required env:
  DRAGONFLY_ACCESS_TOKEN=...
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=@channel_username  # or numeric channel id

Dry-run examples:
  DRAGONFLY_ACCESS_TOKEN=... python3 dragonfly_telegram_poster.py backfill --count 1000 --dry-run
  DRAGONFLY_ACCESS_TOKEN=... python3 dragonfly_telegram_poster.py watch --dry-run
"""
from __future__ import annotations

import argparse
import base64
import html
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
import traceback
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

BASE_URL = "https://dragonfly-flash.ru"
API_FEED = BASE_URL + "/api/feed?type={feed_type}&limit={limit}&offset={offset}"
API_POST = BASE_URL + "/api/post/{post_id}"
API_COMMENTS = BASE_URL + "/api/get_comments/{post_id}?user_id={user_id}"
DEFAULT_DB = Path.home() / ".hermes" / "state" / "dragonfly_telegram_poster.sqlite3"
TG_API = "https://api.telegram.org/bot{token}/{method}"
MAX_TG_CAPTION = 1024
MAX_TG_MESSAGE = 4096
MAX_MEDIA_GROUP = 10
STATS_FOOTER_RESERVE = 80
PART_PLACEHOLDER_TOTAL = "999"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_KEEP_SENT = 50_000
DEFAULT_LOG_FILE = Path.home() / ".hermes" / "logs" / "dragonfly_telegram_poster.log"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
DEFAULT_API_429_BASE_SLEEP = 30.0
DEFAULT_API_429_MAX_SLEEP = 900.0


@dataclass
class Config:
    dragonfly_token: str | None
    dragonfly_user_id: str | None
    telegram_token: str | None
    telegram_chat_id: str | None
    db_path: Path
    dry_run: bool
    feed_type: str
    request_delay: float
    send_delay: float
    text_delay: float
    photo_delay: float
    album_delay: float
    animation_delay: float
    mixed_media_delay: float
    media_item_delay: float
    poll_interval: float
    limit: int
    max_attempts: int
    keep_sent: int
    log_file: str | None
    upload_media: bool
    alert_chat_id: str | None
    discussion_chat_id: str | None
    best_chat_id: str | None
    cookie_file: str | None
    best_likes_threshold: int = 7
    accounts_file: str | None = None
    account_name: str | None = None
    account_pinned: bool = False


LOGGER = logging.getLogger("dragonfly_telegram_poster")


def setup_logging(log_file: str | None = str(DEFAULT_LOG_FILE), verbose: bool = False) -> None:
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(fmt)
    LOGGER.addHandler(stream)

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        LOGGER.addHandler(fh)


def log(msg: str, level: int = logging.INFO) -> None:
    if not LOGGER.handlers:
        setup_logging(None)
    LOGGER.log(level, msg)


def log_exception(msg: str, exc: BaseException) -> None:
    log(f"{msg}: {exc}\n{traceback.format_exc()}", logging.ERROR)


def is_transient_network_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "handshake operation timed out",
            "urlopen error",
            "timed out",
            "ssl eof",
            "temporarily unavailable",
            "connection reset",
            "network error",
        )
    )


def human_duration(seconds: float) -> str:
    seconds_i = int(round(seconds))
    if seconds_i < 60:
        return f"{seconds_i} сек"
    minutes = seconds_i // 60
    rem = seconds_i % 60
    if rem:
        return f"{minutes} мин {rem} сек"
    return f"{minutes} мин"


def send_alert(cfg: Config, title: str, body: str, *, level: str = "warning") -> None:
    """Send a compact user-friendly operational alert to a private Telegram chat.

    Never raises: alerts must not break the parser.
    """
    if not cfg.alert_chat_id or not cfg.telegram_token:
        return
    icon = {"info": "ℹ️", "warning": "⚠️", "error": "🚨", "success": "✅"}.get(level, "⚠️")
    text = (
        f"{icon} <b>{escape_text(title)}</b>\n\n"
        f"{escape_text(body)}\n\n"
        f"<i>{escape_text(datetime.now().strftime('%d.%m.%Y %H:%M:%S'))}</i>"
    )
    try:
        tg_request(cfg, "sendMessage", {
            "chat_id": cfg.alert_chat_id,
            "text": text[:MAX_TG_MESSAGE],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
    except Exception as e:
        log(f"alert delivery failed: {e}", logging.WARNING)


def require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return v


def optional_env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def load_env_file(path: str | None) -> None:
    """Load simple KEY=VALUE lines into os.environ without external deps.

    Existing environment variables win over values from the file.
    """
    if not path:
        return
    p = Path(path).expanduser()
    if not p.exists():
        raise SystemExit(f"Env file not found: {p}")
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value



class DragonflyAuthExpired(RuntimeError):
    """Raised when Dragonfly returns 401 for the active account."""


def load_accounts_file(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        raise SystemExit(f"Dragonfly accounts file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit(f"Dragonfly accounts file has no accounts: {p}")
    return data


def save_accounts_file(path: str, data: dict[str, Any]) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        p.chmod(0o600)
    except Exception:
        pass


def _enabled_accounts(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in data.get("accounts", []) if isinstance(a, dict) and a.get("enabled", True) and a.get("access_token")]


def configure_active_account(cfg: Config) -> None:
    """Load active account token from cfg.accounts_file into cfg.

    Multi-account mode uses the token directly. It intentionally disables the
    single cookie jar path for API calls, because one cookie jar cannot represent
    multiple accounts safely.
    """
    data = load_accounts_file(cfg.accounts_file)
    if not data:
        return
    accounts = _enabled_accounts(data)
    if not accounts:
        raise SystemExit("Dragonfly accounts file has no enabled accounts")
    requested_name = cfg.account_name
    active_name = requested_name or data.get("active")
    account = next((a for a in accounts if a.get("name") == active_name), None)
    if account is None:
        names = ", ".join(str(a.get("name")) for a in accounts)
        raise SystemExit(f"Dragonfly account not found/enabled: {active_name}. Available: {names}")
    cfg.dragonfly_token = str(account["access_token"])
    cfg.cookie_file = None
    cfg.account_name = str(account.get("name") or account.get("sub") or "account")
    cfg.account_pinned = bool(requested_name)


def switch_dragonfly_account(cfg: Config, reason: str) -> bool:
    """Switch to the next enabled account after an auth failure.

    Returns True if switched, False when no replacement exists. This is only for
    auth failures such as 401, not for 429 rate limits.
    """
    if not cfg.accounts_file or cfg.account_pinned:
        return False
    data = load_accounts_file(cfg.accounts_file)
    if not data:
        return False
    accounts = _enabled_accounts(data)
    if len(accounts) <= 1:
        return False
    current = cfg.account_name or data.get("active")
    now = datetime.now(timezone.utc).isoformat()
    for a in data.get("accounts", []):
        if isinstance(a, dict) and a.get("name") == current:
            a["last_error"] = reason[:300]
            a["last_failed_at"] = now
    names = [str(a.get("name") or a.get("sub") or i) for i, a in enumerate(accounts)]
    try:
        start = names.index(str(current)) + 1
    except ValueError:
        start = 0
    for i in range(len(accounts)):
        candidate = accounts[(start + i) % len(accounts)]
        name = str(candidate.get("name") or candidate.get("sub") or "account")
        if name == current:
            continue
        data["active"] = name
        save_accounts_file(cfg.accounts_file, data)
        old = cfg.account_name or "unknown"
        cfg.dragonfly_token = str(candidate["access_token"])
        cfg.cookie_file = None
        cfg.account_name = name
        log(f"Dragonfly account switched {old} -> {name}: {reason}", logging.WARNING)
        send_alert(
            cfg,
            "Dragonfly переключил аккаунт",
            f"Аккаунт {old} получил ошибку авторизации. Переключился на {name}. Причина: {reason[:300]}",
            level="warning",
        )
        return True
    return False

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_posts (
            post_id INTEGER PRIMARY KEY,
            created_at TEXT,
            sent_at TEXT NOT NULL,
            author_name TEXT,
            had_photos INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'sent',
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        )
        """
    )
    # Lightweight migrations for databases created by older script versions.
    cols = {row[1] for row in con.execute("PRAGMA table_info(sent_posts)")}
    for name, ddl in {
        "status": "ALTER TABLE sent_posts ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'",
        "failed_attempts": "ALTER TABLE sent_posts ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0",
        "last_error": "ALTER TABLE sent_posts ADD COLUMN last_error TEXT",
    }.items():
        if name not in cols:
            con.execute(ddl)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_messages (
            post_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'main',
            chat_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            message_kind TEXT NOT NULL,
            base_html TEXT NOT NULL,
            base_hash TEXT NOT NULL,
            last_likes INTEGER,
            last_comments INTEGER,
            last_synced_at TEXT,
            can_edit INTEGER NOT NULL DEFAULT 1,
            last_error TEXT,
            PRIMARY KEY(post_id, role)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_discussion_messages (
            post_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'last',
            channel_chat_id TEXT NOT NULL,
            channel_message_id INTEGER NOT NULL,
            discussion_chat_id TEXT NOT NULL,
            discussion_message_id INTEGER NOT NULL,
            found_at TEXT NOT NULL,
            PRIMARY KEY(post_id, role)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS best_posts (
            post_id INTEGER PRIMARY KEY,
            source_chat_id TEXT NOT NULL,
            source_message_ids TEXT NOT NULL,
            best_chat_id TEXT NOT NULL,
            best_message_ids TEXT NOT NULL,
            likes_at_send INTEGER NOT NULL,
            sent_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS dragonfly_comments (
            post_id INTEGER NOT NULL,
            comment_id INTEGER NOT NULL,
            parent_id INTEGER,
            user_id INTEGER,
            username TEXT,
            text_hash TEXT NOT NULL,
            telegram_chat_id TEXT,
            telegram_message_id INTEGER,
            sent_at TEXT NOT NULL,
            PRIMARY KEY(post_id, comment_id)
        )
        """
    )
    cols_tm = {row[1] for row in con.execute("PRAGMA table_info(telegram_messages)")}
    for name, ddl in {
        "base_hash": "ALTER TABLE telegram_messages ADD COLUMN base_hash TEXT NOT NULL DEFAULT ''",
        "last_likes": "ALTER TABLE telegram_messages ADD COLUMN last_likes INTEGER",
        "last_comments": "ALTER TABLE telegram_messages ADD COLUMN last_comments INTEGER",
        "last_synced_at": "ALTER TABLE telegram_messages ADD COLUMN last_synced_at TEXT",
        "can_edit": "ALTER TABLE telegram_messages ADD COLUMN can_edit INTEGER NOT NULL DEFAULT 1",
        "last_error": "ALTER TABLE telegram_messages ADD COLUMN last_error TEXT",
    }.items():
        if name not in cols_tm:
            con.execute(ddl)
    con.commit()
    return con


def is_sent(con: sqlite3.Connection, post_id: int) -> bool:
    row = con.execute("SELECT 1 FROM sent_posts WHERE post_id = ? AND status = 'sent'", (post_id,)).fetchone()
    return row is not None


def is_exhausted(con: sqlite3.Connection, post_id: int, max_attempts: int) -> bool:
    row = con.execute(
        "SELECT failed_attempts, status FROM sent_posts WHERE post_id = ?",
        (post_id,),
    ).fetchone()
    return bool(row and row[1] == "failed" and int(row[0]) >= max_attempts)


def failed_attempts(con: sqlite3.Connection, post_id: int) -> int:
    row = con.execute(
        "SELECT failed_attempts, status FROM sent_posts WHERE post_id = ?",
        (int(post_id),),
    ).fetchone()
    if not row or row[1] != "failed":
        return 0
    return int(row[0] or 0)


def mark_sent(con: sqlite3.Connection, post: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO sent_posts(post_id, created_at, sent_at, author_name, had_photos, status, failed_attempts, last_error)
        VALUES(?, ?, ?, ?, ?, 'sent', 0, NULL)
        """,
        (
            int(post["post_id"]),
            post.get("created_at"),
            datetime.now(timezone.utc).isoformat(),
            post.get("author_name"),
            1 if post.get("photos") else 0,
        ),
    )
    con.commit()


def _html_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _message_id_from_result(result: Any) -> int | None:
    if isinstance(result, dict):
        value = result.get("message_id")
        if isinstance(value, int):
            return value
    return None


def _message_id_from_tg_response(resp: dict[str, Any]) -> int | None:
    result = resp.get("result") if isinstance(resp, dict) else None
    if isinstance(result, list) and result:
        return _message_id_from_result(result[0])
    return _message_id_from_result(result)


def _last_message_id_from_tg_response(resp: dict[str, Any]) -> int | None:
    result = resp.get("result") if isinstance(resp, dict) else None
    if isinstance(result, list) and result:
        return _message_id_from_result(result[-1])
    return _message_id_from_result(result)


def extract_post_stats(post: dict[str, Any]) -> tuple[int | None, int | None]:
    def as_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            for key in ("count", "total", "total_count"):
                if key in value:
                    return as_int(value.get(key))
            return None
        try:
            return int(str(value).strip())
        except Exception:
            return None

    likes = None
    comments = None
    for key in ("likes_count", "like_count", "likes", "likes_total", "reactions_count"):
        if key in post:
            likes = as_int(post.get(key))
            if likes is not None:
                break
    for key in ("comments_count", "comment_count", "comments", "comments_total", "replies_count"):
        if key in post:
            comments = as_int(post.get(key))
            if comments is not None:
                break
    return likes, comments


def stats_footer_html(likes: int | None, comments: int | None) -> str:
    l = 0 if likes is None else int(likes)
    c = 0 if comments is None else int(comments)
    return f"❤️ {l}   💬 {c}"


def append_stats_footer(base_html: str, likes: int | None, comments: int | None, *, limit: int) -> str | None:
    footer = "\n\n" + stats_footer_html(likes, comments)
    body = base_html.rstrip() + footer
    if len(body) <= limit:
        return body
    return None


def format_chunks_for_send(post: dict[str, Any], *, limit: int) -> list[str]:
    safe_limit = max(1, limit - STATS_FOOTER_RESERVE)
    return format_html_chunks(post, limit=safe_limit)


def chunks_with_initial_stats(post: dict[str, Any], chunks: list[str], *, limit: int) -> list[str]:
    """Add current ❤️/💬 footer to the first/main Telegram chunk on initial send.

    The database still stores the base chunk without footer, so later stats edits
    can rebuild the footer cleanly instead of stacking duplicate lines.
    """
    if not chunks:
        return chunks
    likes, comments = extract_post_stats(post)
    first = append_stats_footer(chunks[0], likes, comments, limit=limit)
    if first is None:
        log(f"initial stats footer skipped for post #{post.get('post_id')}: Telegram limit={limit}", logging.WARNING)
        return chunks
    return [first, *chunks[1:]]


def record_telegram_message(
    con: sqlite3.Connection,
    post: dict[str, Any],
    *,
    chat_id: str,
    message_id: int | None,
    message_kind: str,
    base_html: str,
    role: str = "main",
) -> None:
    if message_id is None:
        return
    likes, comments = extract_post_stats(post)
    con.execute(
        """
        INSERT OR REPLACE INTO telegram_messages(
            post_id, role, chat_id, message_id, message_kind, base_html, base_hash,
            last_likes, last_comments, last_synced_at, can_edit, last_error
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
        """,
        (
            int(post["post_id"]),
            role,
            str(chat_id),
            int(message_id),
            message_kind,
            base_html,
            _html_hash(base_html),
            likes,
            comments,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()


def get_main_telegram_message(con: sqlite3.Connection, post_id: int) -> sqlite3.Row | None:
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM telegram_messages WHERE post_id = ? AND role = 'main' AND can_edit = 1",
            (int(post_id),),
        ).fetchone()
    finally:
        con.row_factory = old_factory
    return row


def update_telegram_message_stats(con: sqlite3.Connection, post_id: int, likes: int | None, comments: int | None) -> None:
    con.execute(
        """
        UPDATE telegram_messages
        SET last_likes = ?, last_comments = ?, last_synced_at = ?, last_error = NULL
        WHERE post_id = ? AND role = 'main'
        """,
        (likes, comments, datetime.now(timezone.utc).isoformat(), int(post_id)),
    )
    con.commit()


def mark_telegram_message_uneditable(con: sqlite3.Connection, post_id: int, error: str) -> None:
    con.execute(
        "UPDATE telegram_messages SET can_edit = 0, last_error = ? WHERE post_id = ? AND role = 'main'",
        (error[:1000], int(post_id)),
    )
    con.commit()


def get_telegram_message(con: sqlite3.Connection, post_id: int, role: str) -> sqlite3.Row | None:
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM telegram_messages WHERE post_id = ? AND role = ?",
            (int(post_id), str(role)),
        ).fetchone()
    finally:
        con.row_factory = old_factory
    return row


def is_best_post_sent(con: sqlite3.Connection, post_id: int) -> bool:
    return con.execute("SELECT 1 FROM best_posts WHERE post_id = ?", (int(post_id),)).fetchone() is not None


def record_best_post(
    con: sqlite3.Connection,
    *,
    post_id: int,
    source_chat_id: str,
    source_message_ids: list[int],
    best_chat_id: str,
    best_message_ids: list[int],
    likes_at_send: int,
) -> None:
    con.execute(
        """
        INSERT OR IGNORE INTO best_posts(
            post_id, source_chat_id, source_message_ids, best_chat_id,
            best_message_ids, likes_at_send, sent_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(post_id),
            str(source_chat_id),
            json.dumps([int(x) for x in source_message_ids], ensure_ascii=False),
            str(best_chat_id),
            json.dumps([int(x) for x in best_message_ids], ensure_ascii=False),
            int(likes_at_send),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()


def kv_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def kv_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, value))
    con.commit()


def tg_get_updates(cfg: Config, con: sqlite3.Connection, *, timeout: int = 2) -> list[dict[str, Any]]:
    if cfg.dry_run or not cfg.telegram_token:
        return []
    payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "channel_post"]}
    offset_s = kv_get(con, "telegram_updates_offset")
    if offset_s:
        try:
            payload["offset"] = int(offset_s)
        except Exception:
            pass
    data = tg_request(cfg, "getUpdates", payload)
    updates = data.get("result") if isinstance(data, dict) else []
    if not isinstance(updates, list):
        return []
    max_update_id: int | None = None
    clean = [u for u in updates if isinstance(u, dict)]
    for upd in clean:
        if isinstance(upd.get("update_id"), int):
            max_update_id = max(max_update_id or 0, int(upd["update_id"]))
    if max_update_id is not None:
        kv_set(con, "telegram_updates_offset", str(max_update_id + 1))
    return clean


def _discussion_message_matches(msg: dict[str, Any], discussion_chat_id: str, channel_message_id: int) -> bool:
    chat = msg.get("chat") or {}
    if str(chat.get("id")) != str(discussion_chat_id):
        return False
    # Bot API legacy fields for automatic channel forwards.
    if msg.get("forward_from_message_id") == channel_message_id:
        return True
    if msg.get("forward_from_message_id") or msg.get("is_automatic_forward"):
        fchat = msg.get("forward_from_chat") or {}
        if msg.get("forward_from_message_id") == channel_message_id and fchat:
            return True
    # Newer Bot API forward_origin shape.
    origin = msg.get("forward_origin") or {}
    if isinstance(origin, dict):
        if origin.get("message_id") == channel_message_id:
            return True
        origin_chat = origin.get("chat") or {}
        if origin_chat and msg.get("is_automatic_forward") and origin.get("message_id") == channel_message_id:
            return True
    return False


def save_discussion_message(
    con: sqlite3.Connection,
    *,
    post_id: int,
    role: str,
    channel_chat_id: str,
    channel_message_id: int,
    discussion_chat_id: str,
    discussion_message_id: int,
) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO telegram_discussion_messages(
            post_id, role, channel_chat_id, channel_message_id, discussion_chat_id, discussion_message_id, found_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(post_id), role, str(channel_chat_id), int(channel_message_id),
            str(discussion_chat_id), int(discussion_message_id), datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()


def try_capture_discussion_mapping(
    cfg: Config,
    con: sqlite3.Connection,
    *,
    post_id: int,
    role: str,
    channel_message_id: int | None,
    wait_seconds: float = 8.0,
) -> int | None:
    if not cfg.discussion_chat_id or not channel_message_id:
        return None
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            updates = tg_get_updates(cfg, con, timeout=2)
        except Exception as e:
            log(f"discussion mapping getUpdates failed post #{post_id}: {e}", logging.WARNING)
            return None
        for upd in updates:
            msg = upd.get("message") or upd.get("channel_post") or {}
            if not isinstance(msg, dict):
                continue
            if _discussion_message_matches(msg, str(cfg.discussion_chat_id), int(channel_message_id)):
                did = msg.get("message_id")
                if isinstance(did, int):
                    save_discussion_message(
                        con,
                        post_id=int(post_id),
                        role=role,
                        channel_chat_id=str(cfg.telegram_chat_id),
                        channel_message_id=int(channel_message_id),
                        discussion_chat_id=str(cfg.discussion_chat_id),
                        discussion_message_id=int(did),
                    )
                    log(f"discussion mapping saved post #{post_id} role={role} channel_message_id={channel_message_id} discussion_message_id={did}")
                    return did
        if time.monotonic() >= deadline:
            log(f"discussion mapping not found yet post #{post_id} role={role} channel_message_id={channel_message_id}", logging.DEBUG)
            return None
        time.sleep(0.5)


def mark_failed(con: sqlite3.Connection, post: dict[str, Any], error: str) -> None:
    pid = int(post["post_id"])
    existing = con.execute("SELECT failed_attempts FROM sent_posts WHERE post_id = ?", (pid,)).fetchone()
    attempts = (int(existing[0]) if existing else 0) + 1
    con.execute(
        """
        INSERT OR REPLACE INTO sent_posts(post_id, created_at, sent_at, author_name, had_photos, status, failed_attempts, last_error)
        VALUES(?, ?, ?, ?, ?, 'failed', ?, ?)
        """,
        (
            pid,
            post.get("created_at"),
            datetime.now(timezone.utc).isoformat(),
            post.get("author_name"),
            1 if post.get("photos") else 0,
            attempts,
            error[:1000],
        ),
    )
    con.commit()


def cleanup_sent(con: sqlite3.Connection, keep_sent: int = DEFAULT_KEEP_SENT) -> int:
    """Delete old successful sent rows, keeping newest keep_sent by post_id.

    Failed rows are preserved for diagnostics/retry history.
    """
    if keep_sent <= 0:
        cur = con.execute("DELETE FROM sent_posts WHERE status = 'sent'")
        con.commit()
        return int(cur.rowcount or 0)
    cur = con.execute(
        """
        DELETE FROM sent_posts
        WHERE status = 'sent'
          AND post_id NOT IN (
              SELECT post_id FROM sent_posts
              WHERE status = 'sent'
              ORDER BY post_id DESC
              LIMIT ?
          )
        """,
        (int(keep_sent),),
    )
    con.commit()
    return int(cur.rowcount or 0)


def mark_seen_without_sending(con: sqlite3.Connection, posts: Iterable[dict[str, Any]]) -> int:
    n = 0
    for p in posts:
        if isinstance(p.get("post_id"), int) and not is_sent(con, int(p["post_id"])):
            mark_sent(con, p)
            n += 1
    return n


def dragonfly_headers(cfg: Config) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "dragonfly-telegram-poster/0.3",
    }
    # Cookie session is preferred. When configured, do not also send the legacy
    # Bearer JWT; Dragonfly's frontend has moved to HttpOnly-cookie sessions.
    if cfg.cookie_file:
        return headers
    if cfg.dragonfly_token:
        headers["Authorization"] = f"Bearer {cfg.dragonfly_token}"
    return headers


def dragonfly_opener(cfg: Config):
    if not cfg.cookie_file:
        return None
    path = Path(cfg.cookie_file).expanduser()
    jar = http.cookiejar.MozillaCookieJar(str(path))
    if path.exists():
        jar.load(ignore_discard=True, ignore_expires=True)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def dragonfly_auth_alert(cfg: Config) -> Callable[[str], None]:
    return lambda msg: send_alert(cfg, "Авторизация Dragonfly истекла", msg, level="error")


def api_get_json(
    url: str,
    headers: dict[str, str],
    timeout: int = 30,
    retries: int = 4,
    alert: Callable[[str], None] | None = None,
    auth_alert: Callable[[str], None] | None = None,
    opener: Any | None = None,
) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            open_fn = opener.open if opener is not None else urllib.request.urlopen
            with open_fn(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                msg = "Авторизация Dragonfly истекла. Сайт вернул 401: cookie/JWT больше не работает. Нужно обновить авторизацию, иначе новые посты получать не получится."
                if auth_alert:
                    auth_alert(msg)
                raise DragonflyAuthExpired(f"HTTP 401 for {url}: {body[:500]}") from e
            if e.code == 429 and attempt < retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                sleep_for = min(DEFAULT_API_429_MAX_SLEEP, DEFAULT_API_429_BASE_SLEEP * (2 ** attempt))
                try:
                    if retry_after:
                        sleep_for = max(sleep_for, float(retry_after))
                except Exception:
                    pass
                last_error = RuntimeError(f"HTTP 429 for {url}: {body[:500]}")
                log(f"Dragonfly rate limit 429; sleeping {sleep_for:.1f}s before retry attempt={attempt + 1}/{retries} url={url}", logging.WARNING)
                if alert:
                    alert(f"Получили 429 от Dragonfly. жду {human_duration(sleep_for)} и пытаюсь снова — попытка {attempt + 1}/{retries}.")
                time.sleep(sleep_for)
                continue
            if e.code < 500 or attempt >= retries:
                raise RuntimeError(f"HTTP {e.code} for {url}: {body[:500]}") from e
            last_error = RuntimeError(f"HTTP {e.code} for {url}: {body[:500]}")
        except urllib.error.URLError as e:
            last_error = e
            if attempt >= retries:
                raise RuntimeError(f"Network error for {url}: {e}") from e
        sleep_for = min(30.0, 2.0 * (2 ** attempt))
        log(f"retry api_get_json attempt={attempt + 1}/{retries} sleep={sleep_for:.1f}s url={url} error={last_error}", logging.WARNING)
        time.sleep(sleep_for)
    raise RuntimeError(f"Network error for {url}: {last_error}")



def api_get_json_dragonfly(cfg: Config, url: str) -> dict[str, Any]:
    attempts = 0
    max_attempts = 1
    if cfg.accounts_file:
        data = load_accounts_file(cfg.accounts_file) or {}
        max_attempts = max(1, len(_enabled_accounts(data)))
    while True:
        try:
            return api_get_json(
                url,
                headers=dragonfly_headers(cfg),
                auth_alert=dragonfly_auth_alert(cfg),
                opener=dragonfly_opener(cfg),
            )
        except DragonflyAuthExpired as e:
            attempts += 1
            if attempts >= max_attempts or not switch_dragonfly_account(cfg, str(e)):
                raise
            log(f"retrying Dragonfly request with account={cfg.account_name} url={url}", logging.WARNING)

def _jwt_payload(token: str | None) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode("utf-8"))
    except Exception:
        return {}


def dragonfly_user_id(cfg: Config) -> str | None:
    if cfg.dragonfly_user_id:
        return str(cfg.dragonfly_user_id)
    payload = _jwt_payload(cfg.dragonfly_token)
    sub = payload.get("sub")
    return str(sub) if sub is not None else None


def fetch_feed_page(cfg: Config, offset: int) -> list[dict[str, Any]]:
    url = API_FEED.format(feed_type=cfg.feed_type, limit=cfg.limit, offset=offset)
    log(f"fetch feed page offset={offset} limit={cfg.limit} type={cfg.feed_type}", logging.DEBUG)
    data = api_get_json_dragonfly(cfg, url)
    feed = data.get("feed") or []
    log(f"fetched feed page offset={offset} posts={len(feed)}", logging.DEBUG)
    return [p for p in feed if isinstance(p, dict) and isinstance(p.get("post_id"), int)]


def fetch_post_by_id(cfg: Config, post_id: int) -> dict[str, Any] | None:
    url = API_POST.format(post_id=int(post_id))
    log(f"fetch post by id #{post_id}", logging.DEBUG)
    try:
        data = api_get_json_dragonfly(cfg, url)
    except RuntimeError as e:
        msg = str(e)
        if "HTTP 404" in msg or "HTTP 422" in msg:
            log(f"post #{post_id} not available: {e}", logging.DEBUG)
            return None
        raise
    candidate = data.get("post") if isinstance(data, dict) else None
    if not isinstance(candidate, dict):
        candidate = data if isinstance(data, dict) else None
    if isinstance(candidate, dict) and isinstance(candidate.get("post_id"), int):
        return candidate
    return None


def fetch_recent_posts(cfg: Config, count: int, *, start_offset: int = 0) -> list[dict[str, Any]]:
    """Fetch up to count posts using offset pagination. Returns newest-first from API order."""
    posts: list[dict[str, Any]] = []
    seen: set[int] = set()
    offset = max(0, int(start_offset))
    while len(posts) < count:
        page = fetch_feed_page(cfg, offset)
        if not page:
            break
        for p in page:
            pid = int(p["post_id"])
            if pid not in seen:
                posts.append(p)
                seen.add(pid)
                if len(posts) >= count:
                    break
        if len(page) < cfg.limit:
            break
        offset += cfg.limit
        if cfg.request_delay > 0:
            time.sleep(cfg.request_delay)
    return posts


def abs_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return BASE_URL + "/" + path.lstrip("/")


def parse_time(s: str | None) -> str:
    if not s:
        return ""
    try:
        # Frontend treats API timestamps as UTC by appending Z.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return s


def post_url(post_id: int) -> str:
    return f"{BASE_URL}/?post={post_id}"


def escape_text(value: Any) -> str:
    """Normalize and escape visible Telegram HTML text.

    Dragonfly sometimes returns already-escaped entities like &#x27; for an
    apostrophe. Decode them first, then escape only characters that would break
    Telegram HTML. Apostrophes/quotes in visible text stay readable.
    """
    return html.escape(html.unescape(str(value)), quote=False)


def escape_attr(value: Any) -> str:
    """Escape Telegram HTML attribute values such as href."""
    return html.escape(str(value), quote=True)


def _first_nonempty(d: dict[str, Any], keys: Iterable[str]) -> str:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _format_duration(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        seconds = int(float(str(value)))
    except Exception:
        return str(value).strip()
    if seconds <= 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _basename(value: str) -> str:
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value)
    return Path(parsed.path or value).name


def audio_info_html(post: dict[str, Any]) -> str:
    audios = post.get("audios") or []
    if not isinstance(audios, list) or not audios:
        return ""
    lines: list[str] = []
    for idx, audio in enumerate(audios[:5], start=1):
        if not isinstance(audio, dict):
            continue
        artist = _first_nonempty(audio, ["artist", "author", "performer", "singer", "creator", "owner_name"])
        title = _first_nonempty(audio, ["title", "name", "track", "song", "audio_name", "original_name"])
        duration = _format_duration(_first_nonempty(audio, ["duration", "duration_sec", "duration_seconds", "length", "time"]))
        file_name = _basename(_first_nonempty(audio, ["file_path", "path", "url", "src", "file", "filename"]))

        if artist and title:
            main = f"{artist} — {title}"
        else:
            main = title or artist or file_name or "трек"

        details = []
        if duration:
            details.append(duration)
        suffix = f" ({', '.join(details)})" if details else ""
        prefix = f"{idx}. " if len(audios) > 1 else ""
        lines.append(prefix + escape_text(main + suffix))
    if not lines:
        return ""
    if len(lines) == 1:
        return "🎵 <b>Трек:</b> " + lines[0]
    return "🎵 <b>Музыка:</b>\n" + "\n".join(lines)


def is_publishable(post: dict[str, Any]) -> bool:
    """Return True when a post has text, visual media, or useful audio info."""
    text = (post.get("description") or "").strip()
    return bool(text or photo_urls(post) or audio_info_html(post))


def profile_url(post: dict[str, Any]) -> str | None:
    author_link = str(post.get("author_link") or "").strip()
    if not author_link:
        return None
    if author_link.startswith("http://") or author_link.startswith("https://"):
        return author_link
    return BASE_URL + "/?id=" + urllib.parse.quote(author_link.lstrip("/"), safe="")


def post_header_html(post: dict[str, Any], *, continuation: bool = False) -> str:
    pid = int(post["post_id"])
    if continuation:
        return f"продолжение поста #{pid}"
    author = escape_text(post.get("author_name") or "Пользователь")
    created = escape_text(parse_time(post.get("created_at")))
    url = profile_url(post)
    if url:
        author_html = f'👤 <a href="{escape_attr(url)}">{author}</a>'
    else:
        author_html = f"👤 <b>{author}</b>"
    lines: list[str] = [author_html]
    if created:
        lines.append(f"🕒 <i>{created}</i>")
    return "\n".join(lines)


def post_meta_html(post: dict[str, Any]) -> str:
    parts: list[str] = []
    audio = audio_info_html(post)
    if audio:
        parts.append(audio)
    if post.get("is_repost"):
        parts.append("🔄 репост")
    return "\n".join(parts)


def post_link_html(post: dict[str, Any]) -> str:
    pid = int(post["post_id"])
    return f'<a href="{escape_attr(post_url(pid))}">#{pid}</a>'


def _largest_prefix_that_fits(raw: str, prefix: str, suffix: str, limit: int) -> int:
    """Largest raw prefix length whose escaped rendering fits limit."""
    lo, hi = 0, len(raw)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(prefix + escape_text(raw[:mid].rstrip()) + suffix) <= limit:
            lo = mid
        else:
            hi = mid - 1
    if lo <= 0:
        raise ValueError("Telegram limit too small for post header/footer")

    # Prefer a natural whitespace boundary when it does not waste too much room.
    boundary = max(raw.rfind(" ", 0, lo), raw.rfind("\n", 0, lo))
    if boundary >= int(lo * 0.75):
        return boundary + 1
    return lo


def format_html_chunks(post: dict[str, Any], *, limit: int) -> list[str]:
    """Build one or more Telegram HTML messages/captions within a hard limit.

    First chunk contains author/time context. Continuation chunks start with only
    `продолжение поста #...`; non-final chunks end with a clear part marker;
    the final chunk contains media/repost metadata and the original post link.
    """
    raw_text = (post.get("description") or "").strip()
    meta = post_meta_html(post)
    final_suffix = ""
    if meta:
        final_suffix += "\n\n" + meta
    final_suffix += "\n\n" + post_link_html(post)
    continue_suffix = f"\n\n<i>({{part}}/{PART_PLACEHOLDER_TOTAL})</i>"

    if not raw_text:
        body = post_header_html(post) + final_suffix
        if len(body) <= limit:
            return [body]
        # Extremely defensive: this should not happen with current header sizes.
        return [body[: max(0, limit - 1)].rstrip() + "…"]

    chunks: list[str] = []
    remaining = raw_text
    first = True
    while remaining:
        prefix = post_header_html(post, continuation=not first) + "\n\n"
        final_part_suffix = f"\n\n<i>({{part}}/{PART_PLACEHOLDER_TOTAL})</i>" + final_suffix
        if len(prefix + escape_text(remaining) + final_part_suffix) <= limit:
            chunks.append(prefix + escape_text(remaining) + final_part_suffix)
            break

        part_no = len(chunks) + 1
        this_continue_suffix = continue_suffix.replace("{part}", str(part_no))
        n = _largest_prefix_that_fits(remaining, prefix, this_continue_suffix, limit)
        part = remaining[:n].rstrip()
        chunks.append(prefix + escape_text(part) + this_continue_suffix)
        remaining = remaining[n:].lstrip()
        first = False

    if len(chunks) <= 1:
        return [chunks[0].replace(f"\n\n<i>({{part}}/{PART_PLACEHOLDER_TOTAL})</i>", "")]

    total = str(len(chunks))
    numbered = []
    for i, chunk in enumerate(chunks, start=1):
        chunk = chunk.replace("{part}", str(i)).replace(PART_PLACEHOLDER_TOTAL, total)
        numbered.append(chunk)
    return numbered


def format_html(post: dict[str, Any], *, caption: bool = False) -> str:
    """Backward-compatible single chunk formatter.

    New sending code uses format_html_chunks() so long posts are split instead
    of truncated.
    """
    return format_html_chunks(post, limit=MAX_TG_CAPTION if caption else MAX_TG_MESSAGE)[0]


def photo_urls(post: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for ph in post.get("photos") or []:
        u = abs_url(ph.get("url"))
        if u:
            urls.append(u)
    return urls


def is_gif_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(".gif")


def tg_request(cfg: Config, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    log(f"telegram request method={method} chat_id={payload.get('chat_id')} dry_run={cfg.dry_run}", logging.DEBUG)
    if cfg.dry_run:
        log(f"DRY-RUN Telegram {method}: {json.dumps(payload, ensure_ascii=False)[:1200]}")
        return {"ok": True, "result": {"dry_run": True}}
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required unless --dry-run")

    url = TG_API.format(token=cfg.telegram_token, method=method)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "dragonfly-telegram-poster/0.2"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        # Telegram flood wait is often HTTP 429 with retry_after.
        try:
            err = json.loads(raw)
            retry = int(((err.get("parameters") or {}).get("retry_after") or 0))
        except Exception:
            retry = 0
        if e.code == 429 and retry > 0:
            sleep_for = retry + 2
            log(f"Telegram flood wait: sleeping {sleep_for}s")
            if str(payload.get("chat_id")) != str(cfg.alert_chat_id):
                send_alert(
                    cfg,
                    "Telegram попросил притормозить",
                    f"Получили 429 от Telegram. Жду {human_duration(sleep_for)} и продолжаю отправку. Метод: {method}.",
                    level="warning",
                )
            time.sleep(sleep_for)
            return tg_request(cfg, method, payload)
        raise RuntimeError(f"Telegram HTTP {e.code}: {raw[:1000]}") from e
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def tg_multipart_request(cfg: Config, method: str, fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> dict[str, Any]:
    log(f"telegram multipart method={method} chat_id={fields.get('chat_id')} files={list(files)} dry_run={cfg.dry_run}", logging.DEBUG)
    if cfg.dry_run:
        safe = {"fields": fields, "files": {k: {"filename": v[0], "bytes": len(v[1]), "content_type": v[2]} for k, v in files.items()}}
        log(f"DRY-RUN Telegram multipart {method}: {json.dumps(safe, ensure_ascii=False)[:1200]}")
        return {"ok": True, "result": {"dry_run": True}}
    if not cfg.telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required unless --dry-run")

    boundary = "----dragonflyBoundary" + str(int(time.time() * 1000000))
    body = bytearray()
    for k, v in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        body.extend(str(v).encode("utf-8"))
        body.extend(b"\r\n")
    for field, (filename, content, ctype) in files.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode())
        body.extend(f"Content-Type: {ctype}\r\n\r\n".encode())
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        TG_API.format(token=cfg.telegram_token, method=method),
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "dragonfly-telegram-poster/0.2"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(raw)
            retry = int(((err.get("parameters") or {}).get("retry_after") or 0))
        except Exception:
            retry = 0
        if e.code == 429 and retry > 0:
            sleep_for = retry + 2
            log(f"Telegram flood wait: sleeping {sleep_for}s")
            if str(fields.get("chat_id")) != str(cfg.alert_chat_id):
                send_alert(
                    cfg,
                    "Telegram попросил притормозить",
                    f"Получили 429 от Telegram. Жду {human_duration(sleep_for)} и продолжаю отправку медиа. Метод: {method}.",
                    level="warning",
                )
            time.sleep(sleep_for)
            return tg_multipart_request(cfg, method, fields, files)
        raise RuntimeError(f"Telegram HTTP {e.code}: {raw[:1000]}") from e
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def download_media_bytes(url: str, retries: int = 3) -> tuple[str, bytes, str]:
    """Download media into memory for Telegram upload; nothing persists on disk.

    Dragonfly media endpoints sometimes throw SSL EOF/handshake timeouts or 5xx.
    Retry those before falling back to text-only publication.
    """
    log(f"download media url={url}", logging.DEBUG)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                ctype = resp.headers.get("content-type") or "application/octet-stream"
                clen = resp.headers.get("content-length")
                if clen and int(clen) > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(f"media too large: {clen} bytes url={url}")
                data = resp.read(MAX_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"media too large: >{MAX_DOWNLOAD_BYTES} bytes url={url}")
            path = urllib.parse.urlparse(url).path
            filename = Path(path).name or ("media.gif" if "gif" in ctype else "media.jpg")
            return filename, data, ctype
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in (429, 500, 502, 503, 504) or attempt >= retries:
                raise
        except urllib.error.URLError as e:
            last_error = e
            if attempt >= retries:
                raise
        sleep_for = min(60.0, 5.0 * (2 ** attempt))
        log(f"retry media download attempt={attempt + 1}/{retries} sleep={sleep_for:.1f}s url={url} error={last_error}", logging.WARNING)
        time.sleep(sleep_for)
    raise RuntimeError(f"media download failed for {url}: {last_error}")


def send_text_chunks(cfg: Config, chunks: list[str], *, start_at: int = 0) -> list[dict[str, Any]]:
    chat_id = cfg.telegram_chat_id
    responses: list[dict[str, Any]] = []
    for chunk in chunks[start_at:]:
        responses.append(tg_request(cfg, "sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }))
        if cfg.send_delay > 0:
            time.sleep(cfg.send_delay)
    return responses


def clean_fallback_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Some Dragonfly feed rows can carry rendered HTML instead of plain post text.
    # Never dump that layout markup into Telegram fallback messages.
    html_markers = ("post-header", "post-footer", "post-photos-grid", "onclick=", "<div", "</div>", "<img")
    tag_count = len(re.findall(r"</?[a-zA-Z][^>]*>", text))
    if any(m in text for m in html_markers) or tag_count >= 3:
        return ""
    return text


def send_media_fallback(cfg: Config, post: dict[str, Any], error: Exception) -> dict[str, Any]:
    fallback = dict(post)
    original_text = clean_fallback_text(fallback.get("description"))
    warning = "⚠️ Медиа не отправилось, поэтому публикую текст и ссылку на оригинал."
    if original_text:
        fallback["description"] = warning + "\n\n" + original_text
    else:
        fallback["description"] = warning
    fallback["photos"] = []
    log(f"media fallback for post #{post.get('post_id')}: {error}")
    chunks = format_chunks_for_send(fallback, limit=MAX_TG_MESSAGE)
    responses = send_text_chunks(cfg, chunks)
    main_resp = responses[0] if responses else {}
    last_resp = responses[-1] if responses else {}
    return {
        "main": {"message_kind": "text", "message_id": _message_id_from_tg_response(main_resp), "base_html": chunks[0] if chunks else ""},
        "last": {"message_kind": "text", "message_id": _last_message_id_from_tg_response(last_resp), "base_html": chunks[-1] if chunks else ""},
    }


def send_one_media(cfg: Config, url: str, *, caption: str | None = None) -> dict[str, Any]:
    chat_id = cfg.telegram_chat_id
    if is_gif_url(url):
        if cfg.upload_media:
            filename, data, ctype = download_media_bytes(url)
            fields = {"chat_id": str(chat_id)}
            if caption:
                fields["caption"] = caption
                fields["parse_mode"] = "HTML"
            return tg_multipart_request(cfg, "sendAnimation", fields, {"animation": (filename, data, ctype)})
        payload: dict[str, Any] = {"chat_id": chat_id, "animation": url}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        return tg_request(cfg, "sendAnimation", payload)
    if cfg.upload_media:
        filename, data, ctype = download_media_bytes(url)
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
            fields["parse_mode"] = "HTML"
        return tg_multipart_request(cfg, "sendPhoto", fields, {"photo": (filename, data, ctype)})
    payload = {"chat_id": chat_id, "photo": url}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    return tg_request(cfg, "sendPhoto", payload)


def send_photo_groups(cfg: Config, photos: list[str], *, caption: str | None = None) -> list[dict[str, Any]]:
    chat_id = cfg.telegram_chat_id
    responses: list[dict[str, Any]] = []
    for group_index, start in enumerate(range(0, len(photos), MAX_MEDIA_GROUP)):
        group = photos[start:start + MAX_MEDIA_GROUP]
        media = []
        files: dict[str, tuple[str, bytes, str]] = {}
        for i, u in enumerate(group):
            media_ref = u
            if cfg.upload_media:
                filename, data, ctype = download_media_bytes(u)
                field = f"photo{group_index}_{i}"
                files[field] = (filename, data, ctype)
                media_ref = f"attach://{field}"
            item: dict[str, Any] = {"type": "photo", "media": media_ref}
            if group_index == 0 and i == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        if cfg.upload_media:
            responses.append(tg_multipart_request(cfg, "sendMediaGroup", {"chat_id": str(chat_id), "media": json.dumps(media, ensure_ascii=False)}, files))
        else:
            responses.append(tg_request(cfg, "sendMediaGroup", {"chat_id": chat_id, "media": media}))
        if cfg.media_item_delay > 0 and start + MAX_MEDIA_GROUP < len(photos):
            time.sleep(cfg.media_item_delay)
    return responses


def send_post(cfg: Config, post: dict[str, Any], *, allow_fallback: bool = True) -> dict[str, Any] | None:
    pid = int(post["post_id"])
    photos = photo_urls(post)
    if not is_publishable(post):
        log(f"skipped empty post #{pid}")
        return None

    if not photos:
        base_chunks = format_chunks_for_send(post, limit=MAX_TG_MESSAGE)
        send_chunks = chunks_with_initial_stats(post, base_chunks, limit=MAX_TG_MESSAGE)
        responses = send_text_chunks(cfg, send_chunks)
        main_resp = responses[0] if responses else {}
        last_resp = responses[-1] if responses else {}
        log(f"sent text post #{pid}")
        return {
            "main": {
                "message_kind": "text",
                "message_id": _message_id_from_tg_response(main_resp),
                "base_html": base_chunks[0] if base_chunks else "",
            },
            "last": {
                "message_kind": "text",
                "message_id": _last_message_id_from_tg_response(last_resp),
                "base_html": base_chunks[-1] if base_chunks else "",
            },
        }

    base_chunks = format_chunks_for_send(post, limit=MAX_TG_CAPTION)
    send_chunks = chunks_with_initial_stats(post, base_chunks, limit=MAX_TG_CAPTION)
    caption = send_chunks[0]
    base_caption = base_chunks[0]

    try:
        if len(photos) == 1:
            media_resp = send_one_media(cfg, photos[0], caption=caption)
            text_responses = send_text_chunks(cfg, send_chunks, start_at=1)
            last_is_text = bool(text_responses)
            last_resp = text_responses[-1] if text_responses else media_resp
            log(f"sent {'animation' if is_gif_url(photos[0]) else 'photo'} post #{pid}")
            return {
                "main": {"message_kind": "caption", "message_id": _message_id_from_tg_response(media_resp), "base_html": base_caption},
                "last": {
                    "message_kind": "text" if last_is_text else "caption",
                    "message_id": _last_message_id_from_tg_response(last_resp),
                    "base_html": base_chunks[-1] if last_is_text else base_caption,
                },
            }

        # If there are GIFs mixed with photos, avoid mediaGroup: albums do not support
        # animation reliably. Send media one by one slowly.
        if any(is_gif_url(u) for u in photos):
            first_resp: dict[str, Any] | None = None
            last_media_resp: dict[str, Any] | None = None
            for i, u in enumerate(photos):
                resp = send_one_media(cfg, u, caption=caption if i == 0 else None)
                if i == 0:
                    first_resp = resp
                last_media_resp = resp
                if cfg.media_item_delay > 0:
                    time.sleep(cfg.media_item_delay)
            text_responses = send_text_chunks(cfg, send_chunks, start_at=1)
            last_is_text = bool(text_responses)
            last_resp = text_responses[-1] if text_responses else (last_media_resp or first_resp or {})
            log(f"sent mixed media post #{pid} media={len(photos)}")
            return {
                "main": {"message_kind": "caption", "message_id": _message_id_from_tg_response(first_resp or {}), "base_html": base_caption},
                "last": {
                    "message_kind": "text" if last_is_text else "caption",
                    "message_id": _last_message_id_from_tg_response(last_resp),
                    "base_html": base_chunks[-1] if last_is_text else base_caption,
                },
            }

        # Telegram mediaGroup: max 10 items, caption only on the first item of the
        # first album. Additional albums carry only media; text continues below.
        media_responses = send_photo_groups(cfg, photos, caption=caption)
        text_responses = send_text_chunks(cfg, send_chunks, start_at=1)
        last_is_text = bool(text_responses)
        last_resp = text_responses[-1] if text_responses else (media_responses[0] if media_responses else {})
        log(f"sent album post #{pid} photos={len(photos)}")
        return {
            "main": {
                "message_kind": "caption",
                "message_id": _message_id_from_tg_response(media_responses[0]) if media_responses else None,
                "base_html": base_caption,
            },
            "last": {
                "message_kind": "text" if last_is_text else "caption",
                "message_id": _last_message_id_from_tg_response(last_resp),
                "base_html": base_chunks[-1] if last_is_text else base_caption,
            },
        }
    except Exception as e:
        if not allow_fallback:
            raise
        return send_media_fallback(cfg, post, e)

def delay_for_post(cfg: Config, post: dict[str, Any]) -> float:
    """Choose post-level delay by content type.

    Text can be fast; GIF/animation can be slow. cfg.send_delay remains only a
    backward-compatible default used to initialize type-specific CLI defaults.
    """
    photos = photo_urls(post)
    if not photos:
        return float(cfg.text_delay)
    gif_count = sum(1 for u in photos if is_gif_url(u))
    if gif_count and gif_count != len(photos):
        return float(cfg.mixed_media_delay)
    if gif_count:
        return float(cfg.animation_delay)
    if len(photos) > 1:
        return float(cfg.album_delay)
    return float(cfg.photo_delay)


def send_new_posts(cfg: Config, con: sqlite3.Connection, posts_newest_first: list[dict[str, Any]], *, mark_only: bool = False) -> int:
    # Send oldest -> newest so channel chronology is natural.
    new_posts = [
        p for p in reversed(posts_newest_first)
        if not is_sent(con, int(p["post_id"]))
        and not is_exhausted(con, int(p["post_id"]), cfg.max_attempts)
    ]
    sent = 0
    for p in new_posts:
        pid = int(p["post_id"])
        if not is_publishable(p):
            if not cfg.dry_run:
                mark_sent(con, p)
            log(f"marked empty post #{pid} as seen" if not cfg.dry_run else f"dry-run skipped empty post #{pid}")
            continue
        if mark_only:
            mark_sent(con, p)
            sent += 1
            log(f"marked seen #{pid}")
            continue
        try:
            attempts_before = failed_attempts(con, pid)
            allow_fallback = attempts_before >= max(0, cfg.max_attempts - 1)
            sent_info = send_post(cfg, p, allow_fallback=allow_fallback)
            if not cfg.dry_run:
                mark_sent(con, p)
                if sent_info:
                    # New structured shape records both edit target (main) and
                    # future discussion-comment target (last). Keep a fallback for
                    # older tests/callers that may return the pre-structured shape.
                    records = sent_info if "main" in sent_info else {"main": sent_info, "last": sent_info}
                    last_info = records.get("last") or records.get("main") or {}
                    for role in ("main", "last"):
                        info = records.get(role) or {}
                        record_telegram_message(
                            con,
                            p,
                            chat_id=str(cfg.telegram_chat_id),
                            message_id=info.get("message_id"),
                            message_kind=str(info.get("message_kind") or "text"),
                            base_html=str(info.get("base_html") or ""),
                            role=role,
                        )
                    try_capture_discussion_mapping(
                        cfg,
                        con,
                        post_id=pid,
                        role="last",
                        channel_message_id=last_info.get("message_id"),
                        wait_seconds=4.0,
                    )
            sent += 1
        except Exception as e:
            mark_failed(con, p, str(e))
            log_exception(f"post #{pid} failed", e)
            if is_exhausted(con, pid, cfg.max_attempts):
                log(f"post #{pid} failed permanently after {cfg.max_attempts} attempts: {e}", logging.ERROR)
                send_alert(
                    cfg,
                    f"Пост #{pid} пропущен после {cfg.max_attempts} попыток",
                    f"Не получилось отправить пост. Я записал ошибку в лог и больше не буду блокировать очередь на этом посте. Причина: {str(e)[:500]}",
                    level="error",
                )
            else:
                log(f"post #{pid} failed, will retry later: {e}", logging.WARNING)
                send_alert(
                    cfg,
                    f"Пост #{pid} временно не отправился",
                    f"Попробую позже. Причина: {str(e)[:500]}",
                    level="warning",
                )
            continue
        post_delay = delay_for_post(cfg, p)
        if post_delay > 0:
            time.sleep(post_delay)
    return sent


def catch_up_missing_ids(
    cfg: Config,
    con: sqlite3.Connection,
    *,
    min_id: int,
    max_id: int,
    known_ids: set[int],
    mark_only: bool = False,
    max_gap_scan: int = 5000,
) -> int:
    """Fetch and process missing post IDs inside a known range.

    Feed pagination can omit IDs or a run can be interrupted. This pass uses
    /api/post/<id> as a slower, exact catch-up path so existing missing posts are
    eventually published. Unavailable IDs are skipped without blocking.
    """
    if max_id < min_id:
        return 0
    span = max_id - min_id + 1
    if span > max_gap_scan:
        log(f"gap catch-up skipped: span={span} exceeds max_gap_scan={max_gap_scan}", logging.WARNING)
        return 0

    processed = 0
    checked = 0
    for pid in range(min_id, max_id + 1):
        if pid in known_ids or is_sent(con, pid) or is_exhausted(con, pid, cfg.max_attempts):
            continue
        checked += 1
        try:
            p = fetch_post_by_id(cfg, pid)
        except Exception as e:
            log_exception(f"gap catch-up fetch post #{pid} failed", e)
            continue
        if p is None:
            log(f"gap catch-up post #{pid} not found/available", logging.DEBUG)
        else:
            processed += send_new_posts(cfg, con, [p], mark_only=mark_only)
        if cfg.request_delay > 0:
            time.sleep(cfg.request_delay)
    if checked or processed:
        log(f"gap catch-up done range={min_id}-{max_id} checked={checked} processed={processed}")
    return processed


def cmd_init(cfg: Config, con: sqlite3.Connection, args: argparse.Namespace) -> int:
    posts = fetch_recent_posts(cfg, args.count)
    n = mark_seen_without_sending(con, posts)
    deleted = cleanup_sent(con, cfg.keep_sent)
    max_id = max((int(p["post_id"]) for p in posts), default=None)
    log(f"initialized: fetched={len(posts)} newly_marked={n} max_post_id={max_id} cleanup_deleted={deleted}")
    return 0


def cmd_backfill(cfg: Config, con: sqlite3.Connection, args: argparse.Namespace) -> int:
    posts = fetch_recent_posts(cfg, args.count)
    log(f"backfill fetched={len(posts)} requested={args.count}")
    n = send_new_posts(cfg, con, posts, mark_only=args.mark_only)
    catchup = 0
    if args.catch_up_gaps and posts:
        ids = {int(p["post_id"]) for p in posts}
        catchup = catch_up_missing_ids(
            cfg,
            con,
            min_id=min(ids),
            max_id=max(ids),
            known_ids=ids,
            mark_only=args.mark_only,
            max_gap_scan=args.max_gap_scan,
        )
    deleted = cleanup_sent(con, cfg.keep_sent)
    log(f"backfill done processed={n} catchup_processed={catchup} cleanup_deleted={deleted}")
    return 0


def cmd_watch(cfg: Config, con: sqlite3.Connection, args: argparse.Namespace) -> int:
    log(f"watch started poll_interval={cfg.poll_interval}s send_delay={cfg.send_delay}s dry_run={cfg.dry_run}")
    while True:
        try:
            posts = fetch_recent_posts(cfg, args.page_size)
            n = send_new_posts(cfg, con, posts)
            catchup = 0
            if args.catch_up_gaps and posts:
                ids = {int(p["post_id"]) for p in posts}
                catchup = catch_up_missing_ids(
                    cfg,
                    con,
                    min_id=min(ids),
                    max_id=max(ids),
                    known_ids=ids,
                    max_gap_scan=args.max_gap_scan,
                )
            if n:
                log(f"watch sent new posts: {n}")
            if catchup:
                log(f"watch caught up missing posts: {catchup}")
            if n or catchup:
                deleted = cleanup_sent(con, cfg.keep_sent)
                if deleted:
                    log(f"cleanup deleted old sent rows: {deleted}")
        except Exception as e:
            # Keep process alive on transient site/TG errors.
            log_exception("watch loop error", e)
            send_alert(
                cfg,
                "Ошибка в watch-цикле",
                f"Парсер не остановлен: подожду {human_duration(cfg.poll_interval)} и попробую снова. Причина: {str(e)[:500]}",
                level="error",
            )
        time.sleep(cfg.poll_interval)


def cmd_auth_check(cfg: Config) -> int:
    try:
        posts = fetch_feed_page(cfg, 0)
    except Exception as e:
        log(f"auth-check failed: {e}", logging.ERROR)
        return 1
    mode = f"accounts:{cfg.account_name}" if cfg.accounts_file else ("cookie" if cfg.cookie_file else "bearer-jwt")
    log(f"auth-check OK mode={mode} sample_posts={len(posts)}")
    return 0


def edit_telegram_message(cfg: Config, *, chat_id: str, message_id: int, message_kind: str, html_body: str) -> None:
    if message_kind == "text":
        tg_request(cfg, "editMessageText", {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": html_body,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })
    elif message_kind == "caption":
        tg_request(cfg, "editMessageCaption", {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "caption": html_body,
            "parse_mode": "HTML",
        })
    else:
        raise RuntimeError(f"unsupported Telegram message kind: {message_kind}")


def sync_post_stats(cfg: Config, con: sqlite3.Connection, post: dict[str, Any]) -> bool:
    pid = int(post["post_id"])
    likes, comments = extract_post_stats(post)
    mirrored_comments = con.execute(
        "SELECT COUNT(*) FROM dragonfly_comments WHERE post_id = ? AND telegram_message_id IS NOT NULL",
        (pid,),
    ).fetchone()[0]
    if comments is None:
        comments = int(mirrored_comments) if mirrored_comments else None
    elif mirrored_comments:
        comments = max(int(comments), int(mirrored_comments))
    if likes is None and comments is None:
        log(f"stats skip post #{pid}: counters missing", logging.DEBUG)
        return False
    row = get_main_telegram_message(con, pid)
    if row is None:
        log(f"stats skip post #{pid}: no Telegram message mapping", logging.DEBUG)
        return False
    old_likes = row["last_likes"]
    old_comments = row["last_comments"]
    if old_likes == likes and old_comments == comments:
        return False
    kind = str(row["message_kind"])
    limit = MAX_TG_MESSAGE if kind == "text" else MAX_TG_CAPTION
    html_body = append_stats_footer(str(row["base_html"]), likes, comments, limit=limit)
    if html_body is None:
        err = f"stats footer does not fit Telegram {kind} limit={limit}"
        log(f"stats edit skip post #{pid}: {err}", logging.WARNING)
        mark_telegram_message_uneditable(con, pid, err)
        send_alert(cfg, "Не получилось обновить статистику поста", f"Пост #{pid}: {err}", level="warning")
        return False
    try:
        edit_telegram_message(
            cfg,
            chat_id=str(row["chat_id"]),
            message_id=int(row["message_id"]),
            message_kind=kind,
            html_body=html_body,
        )
    except Exception as e:
        # Telegram can reject edits of old/unchanged/deleted messages. Keep it
        # retryable except for obvious "message is not modified".
        msg = str(e)
        if "message is not modified" not in msg.lower():
            if is_transient_network_error(e):
                log(f"stats edit transient failure post #{pid}: {msg[:500]}", logging.WARNING)
            else:
                log_exception(f"stats edit failed post #{pid}", e)
                send_alert(cfg, "Ошибка обновления статистики", f"Пост #{pid}: {msg[:500]}", level="warning")
            con.execute(
                "UPDATE telegram_messages SET last_error = ? WHERE post_id = ? AND role = 'main'",
                (msg[:1000], pid),
            )
            con.commit()
            return False
    update_telegram_message_stats(con, pid, likes, comments)
    log(f"stats updated post #{pid}: likes {old_likes}->{likes}, comments {old_comments}->{comments}")
    return True


def source_message_ids_for_best(con: sqlite3.Connection, post_id: int) -> tuple[str, list[int]] | None:
    main = get_telegram_message(con, post_id, "main")
    if main is None:
        return None
    ids = [int(main["message_id"])]
    last = get_telegram_message(con, post_id, "last")
    if last is not None and int(last["message_id"]) not in ids:
        ids.append(int(last["message_id"]))
    return str(main["chat_id"]), ids


def forward_to_best(cfg: Config, *, source_chat_id: str, source_message_ids: list[int]) -> list[int]:
    if not cfg.best_chat_id:
        raise RuntimeError("TELEGRAM_BEST_CHAT_ID / --best-chat-id is required for best-post forwarding")
    forwarded_ids: list[int] = []
    for mid in source_message_ids:
        resp = tg_request(
            cfg,
            "forwardMessage",
            {"chat_id": cfg.best_chat_id, "from_chat_id": source_chat_id, "message_id": int(mid)},
        )
        new_mid = _message_id_from_tg_response(resp)
        if new_mid is not None:
            forwarded_ids.append(int(new_mid))
        time.sleep(max(0.0, cfg.send_delay))
    return forwarded_ids


def sync_best_post(cfg: Config, con: sqlite3.Connection, post: dict[str, Any], *, threshold: int | None = None) -> bool:
    pid = int(post["post_id"])
    likes, _comments = extract_post_stats(post)
    threshold = cfg.best_likes_threshold if threshold is None else int(threshold)
    if likes is None or int(likes) < threshold:
        return False
    if is_best_post_sent(con, pid):
        return False
    source = source_message_ids_for_best(con, pid)
    if source is None:
        log(f"best skip post #{pid}: no Telegram message mapping", logging.DEBUG)
        return False
    source_chat_id, source_message_ids = source
    best_ids = forward_to_best(cfg, source_chat_id=source_chat_id, source_message_ids=source_message_ids)
    record_best_post(
        con,
        post_id=pid,
        source_chat_id=source_chat_id,
        source_message_ids=source_message_ids,
        best_chat_id=str(cfg.best_chat_id),
        best_message_ids=best_ids,
        likes_at_send=int(likes),
    )
    log(f"best forwarded post #{pid}: likes={likes} messages={len(source_message_ids)}")
    return True


def cmd_sync_best(cfg: Config, con: sqlite3.Connection, args: argparse.Namespace) -> int:
    if not cfg.best_chat_id:
        raise SystemExit("Set TELEGRAM_BEST_CHAT_ID or --best-chat-id")
    cfg.best_likes_threshold = int(args.threshold)
    old_limit = cfg.limit
    try:
        cfg.limit = max(cfg.limit, int(args.count))
        posts = fetch_recent_posts(cfg, int(args.count), start_offset=int(getattr(args, "offset", 0)))
    finally:
        cfg.limit = old_limit
    sent = 0
    seen = 0
    for p in posts[: int(args.count)]:
        seen += 1
        try:
            if sync_best_post(cfg, con, p, threshold=int(args.threshold)):
                sent += 1
        except Exception as e:
            log_exception(f"best sync failed post #{p.get('post_id')}", e)
            send_alert(cfg, "Ошибка пересылки в Лучшее", f"Пост #{p.get('post_id')}: {str(e)[:500]}", level="warning")
    log(f"sync-best done checked={seen} sent={sent} threshold={int(args.threshold)}")
    return 0


def cmd_sync_best_watch(cfg: Config, con: sqlite3.Connection, args: argparse.Namespace) -> int:
    log(f"sync-best-watch started interval={args.interval}s count={args.count} offset={getattr(args, 'offset', 0)} threshold={args.threshold} account={cfg.account_name or 'default'} dry_run={cfg.dry_run}")
    while True:
        try:
            cmd_sync_best(cfg, con, args)
        except Exception as e:
            log_exception("sync-best-watch loop error", e)
            send_alert(
                cfg,
                "Ошибка sync-best-watch",
                f"Мониторинг Лучшего не остановлен: подожду {human_duration(args.interval)} и попробую снова. Причина: {str(e)[:500]}",
                level="error",
            )
        time.sleep(float(args.interval))


def fetch_post_comments(cfg: Config, post_id: int) -> list[dict[str, Any]]:
    uid = dragonfly_user_id(cfg)
    if not uid:
        raise RuntimeError("Dragonfly user_id is required for comments; set DRAGONFLY_USER_ID or use a JWT/account token with sub")
    url = API_COMMENTS.format(post_id=int(post_id), user_id=urllib.parse.quote(str(uid)))
    data = api_get_json_dragonfly(cfg, url)
    comments = data if isinstance(data, list) else data.get("comments", []) if isinstance(data, dict) else []
    return [c for c in comments if isinstance(c, dict) and c.get("id") is not None]


def get_discussion_message(con: sqlite3.Connection, post_id: int, role: str = "last") -> sqlite3.Row | None:
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM telegram_discussion_messages WHERE post_id = ? AND role = ?",
            (int(post_id), role),
        ).fetchone()
    finally:
        con.row_factory = old_factory
    return row


def get_comment_record(con: sqlite3.Connection, post_id: int, comment_id: int) -> sqlite3.Row | None:
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM dragonfly_comments WHERE post_id = ? AND comment_id = ?",
            (int(post_id), int(comment_id)),
        ).fetchone()
    finally:
        con.row_factory = old_factory
    return row


def is_comment_sent(con: sqlite3.Connection, post_id: int, comment_id: int) -> bool:
    return get_comment_record(con, post_id, comment_id) is not None


def mark_comment_sent(
    con: sqlite3.Connection,
    *,
    post_id: int,
    comment: dict[str, Any],
    telegram_chat_id: str | None,
    telegram_message_id: int | None,
) -> None:
    cid = int(comment["id"])
    parent = comment.get("parent_id")
    user_id = comment.get("user_id")
    con.execute(
        """
        INSERT OR REPLACE INTO dragonfly_comments(
            post_id, comment_id, parent_id, user_id, username, text_hash,
            telegram_chat_id, telegram_message_id, sent_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(post_id), cid,
            int(parent) if parent is not None else None,
            int(user_id) if user_id is not None else None,
            str(comment.get("username") or ""),
            _html_hash(str(comment.get("text") or "")),
            str(telegram_chat_id) if telegram_chat_id is not None else None,
            int(telegram_message_id) if telegram_message_id is not None else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()


def format_comment_html(comment: dict[str, Any], post_id: int) -> str:
    username = comment.get("username") or f"user_{comment.get('user_id', '')}".strip("_") or "пользователь"
    when = parse_time(str(comment.get("created_at") or ""))
    text = escape_text(comment.get("text") or "")
    if len(text) > 3000:
        text = text[:2990].rstrip() + "…"
    reply = "↪️ " if comment.get("parent_id") else ""
    likes = int(comment.get("likes_count") or 0)
    footer = f'\n\n❤️ {likes}   <a href="{escape_attr(post_url(int(post_id)))}">#{int(post_id)}</a>'
    header = f"💬 {reply}<b>{escape_text(username)}</b>"
    if when:
        header += f" · <i>{escape_text(when)}</i>"
    return f"{header}\n{text}{footer}"[:MAX_TG_MESSAGE]


def send_comment_to_discussion(cfg: Config, target: sqlite3.Row, text: str) -> int | None:
    payload = {
        "chat_id": str(target["discussion_chat_id"]),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_to_message_id": int(target["discussion_message_id"]),
        "allow_sending_without_reply": True,
    }
    resp = tg_request(cfg, "sendMessage", payload)
    return _message_id_from_tg_response(resp)


def sync_post_comments(cfg: Config, con: sqlite3.Connection, post: dict[str, Any], *, send_existing: bool = False) -> tuple[int, int]:
    pid = int(post["post_id"])
    target = get_discussion_message(con, pid, "last")
    if target is None:
        return (0, 0)
    comments = fetch_post_comments(cfg, pid)
    comments.sort(key=lambda c: int(c.get("id") or 0))
    known_any = con.execute("SELECT 1 FROM dragonfly_comments WHERE post_id = ? LIMIT 1", (pid,)).fetchone() is not None
    sent = 0
    marked = 0
    for c in comments:
        try:
            cid = int(c["id"])
        except Exception:
            continue
        existing = get_comment_record(con, pid, cid)
        if existing is not None:
            if not send_existing or existing["telegram_message_id"] is not None:
                continue
            msg_id = send_comment_to_discussion(cfg, target, format_comment_html(c, pid))
            mark_comment_sent(con, post_id=pid, comment=c, telegram_chat_id=str(target["discussion_chat_id"]), telegram_message_id=msg_id)
            sent += 1
            continue
        # On first observation of a post, seed existing comments silently unless
        # --send-existing is enabled. This avoids accidental old-comment floods;
        # enabling --send-existing also sends previously seeded rows with NULL
        # telegram_message_id.
        if not send_existing and not known_any:
            mark_comment_sent(con, post_id=pid, comment=c, telegram_chat_id=None, telegram_message_id=None)
            marked += 1
            continue
        msg_id = send_comment_to_discussion(cfg, target, format_comment_html(c, pid))
        mark_comment_sent(con, post_id=pid, comment=c, telegram_chat_id=str(target["discussion_chat_id"]), telegram_message_id=msg_id)
        sent += 1
    if marked:
        log(f"comments seeded post #{pid}: marked_existing={marked}")
    if sent:
        log(f"comments mirrored post #{pid}: sent={sent}")
    return (sent, marked)


def cmd_sync_comments(cfg: Config, con: sqlite3.Connection, args: argparse.Namespace) -> int:
    old_limit = cfg.limit
    cfg.limit = int(args.count)
    try:
        posts = fetch_recent_posts(cfg, int(args.count), start_offset=int(getattr(args, "offset", 0)))
    finally:
        cfg.limit = old_limit
    total_sent = 0
    total_marked = 0
    checked = 0
    for p in posts[: int(args.count)]:
        checked += 1
        try:
            sent, marked = sync_post_comments(cfg, con, p, send_existing=bool(getattr(args, "send_existing", False)))
            total_sent += sent
            total_marked += marked
            if sent > 0:
                try:
                    sync_post_stats(cfg, con, p)
                except Exception as e:
                    log_exception(f"comments-triggered stats sync failed post #{p.get('post_id')}", e)
        except Exception as e:
            log_exception(f"comments sync failed post #{p.get('post_id')}", e)
    log(f"sync-comments done checked={checked} sent={total_sent} marked_existing={total_marked}")
    return 0


def cmd_sync_comments_watch(cfg: Config, con: sqlite3.Connection, args: argparse.Namespace) -> int:
    log(f"sync-comments-watch started interval={args.interval}s count={args.count} offset={getattr(args, 'offset', 0)} account={cfg.account_name or 'default'} dry_run={cfg.dry_run}")
    while True:
        try:
            cmd_sync_comments(cfg, con, args)
        except Exception as e:
            log_exception("sync-comments-watch loop error", e)
            send_alert(cfg, "Ошибка sync-comments-watch", f"Зеркалирование комментариев не остановлено: попробую снова через {human_duration(args.interval)}. Причина: {str(e)[:500]}", level="error")
        time.sleep(float(args.interval))


def cmd_sync_stats(cfg: Config, con: sqlite3.Connection, args: argparse.Namespace) -> int:
    old_limit = cfg.limit
    cfg.limit = int(args.count)
    try:
        posts = fetch_recent_posts(cfg, int(args.count), start_offset=int(getattr(args, "offset", 0)))
    finally:
        cfg.limit = old_limit
    updated = 0
    seen = 0
    for p in posts[: int(args.count)]:
        seen += 1
        try:
            if sync_post_stats(cfg, con, p):
                updated += 1
        except Exception as e:
            log_exception(f"stats sync failed post #{p.get('post_id')}", e)
    log(f"sync-stats done checked={seen} updated={updated}")
    return 0


def cmd_sync_stats_watch(cfg: Config, con: sqlite3.Connection, args: argparse.Namespace) -> int:
    log(f"sync-stats-watch started interval={args.interval}s count={args.count} offset={getattr(args, 'offset', 0)} account={cfg.account_name or 'default'} dry_run={cfg.dry_run}")
    while True:
        try:
            cmd_sync_stats(cfg, con, args)
        except Exception as e:
            log_exception("sync-stats-watch loop error", e)
            send_alert(
                cfg,
                "Ошибка sync-stats-watch",
                f"Мониторинг лайков/комментариев не остановлен: подожду {human_duration(args.interval)} и попробую снова. Причина: {str(e)[:500]}",
                level="error",
            )
        time.sleep(float(args.interval))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Dragonfly Flash feed to Telegram")
    p.add_argument("--env-file", default=None, help="load KEY=VALUE secrets from file before reading env")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--feed-type", default="all", choices=["all", "friends"])
    p.add_argument("--limit", type=int, default=20, help="API page size")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--request-delay", type=float, default=1.0, help="delay between Dragonfly pagination requests")
    p.add_argument("--send-delay", type=float, default=4.0, help="legacy fallback delay; type-specific delays below are preferred")
    p.add_argument("--text-delay", type=float, default=2.0, help="delay after a text-only post")
    p.add_argument("--photo-delay", type=float, default=8.0, help="delay after a single photo post")
    p.add_argument("--album-delay", type=float, default=15.0, help="delay after a photo album post")
    p.add_argument("--animation-delay", type=float, default=45.0, help="delay after GIF/animation post; Telegram rate-limits these harder")
    p.add_argument("--mixed-media-delay", type=float, default=45.0, help="delay after mixed GIF/photo post")
    p.add_argument("--media-item-delay", type=float, default=12.0, help="delay between multiple media items/albums inside one post")
    p.add_argument("--poll-interval", type=float, default=15.0)
    p.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS, help="per-post retry limit before skipping")
    p.add_argument("--keep-sent", type=int, default=DEFAULT_KEEP_SENT, help="keep only newest N successful sent rows; failed rows are preserved")
    p.add_argument("--log-file", default=str(DEFAULT_LOG_FILE), help="write logs to this file; use empty string to disable file logging")
    p.add_argument("--verbose", action="store_true", help="enable debug logging")
    p.add_argument("--url-media", action="store_true", help="send media as URLs instead of uploading downloaded bytes (not recommended for Dragonfly)")
    p.add_argument("--alert-chat-id", default=None, help="send user-friendly warnings/errors to this Telegram user/chat; env TELEGRAM_ALERT_CHAT_ID also works")
    p.add_argument("--discussion-chat-id", default=None, help="linked Telegram discussion group id; env TELEGRAM_DISCUSSION_CHAT_ID also works")
    p.add_argument("--best-chat-id", default=None, help="Telegram channel id for best posts; env TELEGRAM_BEST_CHAT_ID also works")
    p.add_argument("--dragonfly-user-id", default=None, help="Dragonfly user id for /api/get_comments; env DRAGONFLY_USER_ID also works")
    p.add_argument("--cookie-file", default=None, help="Dragonfly Mozilla/Netscape cookie jar; env DRAGONFLY_COOKIE_FILE also works. Preferred over legacy JWT.")
    p.add_argument("--accounts-file", default=None, help="JSON file with Dragonfly account tokens for 401/auth failover; env DRAGONFLY_ACCOUNTS_FILE also works.")
    p.add_argument("--dragonfly-account", default=None, help="pin this process to a named Dragonfly account from --accounts-file; does not rewrite active account")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth-check", help="check Dragonfly authentication and exit")

    s = sub.add_parser("init", help="mark recent posts as seen without sending")
    s.add_argument("--count", type=int, default=20)

    s = sub.add_parser("backfill", help="send recent historical posts")
    s.add_argument("--count", type=int, default=1000)
    s.add_argument("--mark-only", action="store_true", help="mark as sent without sending")
    s.add_argument("--no-catch-up-gaps", dest="catch_up_gaps", action="store_false", default=True, help="disable exact /api/post/<id> gap catch-up")
    s.add_argument("--max-gap-scan", type=int, default=5000, help="largest ID span to scan for missing posts")

    s = sub.add_parser("watch", help="poll forever")
    s.add_argument("--page-size", type=int, default=20)
    s.add_argument("--no-catch-up-gaps", dest="catch_up_gaps", action="store_false", default=True, help="disable exact /api/post/<id> gap catch-up")
    s.add_argument("--max-gap-scan", type=int, default=200, help="largest ID span to scan for missing posts per poll")

    s = sub.add_parser("sync-stats", help="one-shot sync of likes/comments for recent posts already mapped to Telegram")
    s.add_argument("--count", type=int, default=20, help="recent Dragonfly posts to inspect")
    s.add_argument("--offset", type=int, default=0, help="Dragonfly feed offset; useful for sharding read-only watchers")

    s = sub.add_parser("sync-stats-watch", help="poll likes/comments and edit Telegram posts forever")
    s.add_argument("--count", type=int, default=20, help="recent Dragonfly posts to inspect per tick")
    s.add_argument("--interval", type=float, default=60.0, help="seconds between stats sync ticks")
    s.add_argument("--offset", type=int, default=0, help="Dragonfly feed offset; useful for sharding read-only watchers")

    s = sub.add_parser("sync-comments", help="one-shot mirror of new Dragonfly comments into Telegram discussion")
    s.add_argument("--count", type=int, default=20, help="recent Dragonfly posts to inspect")
    s.add_argument("--offset", type=int, default=0, help="Dragonfly feed offset; useful for sharding read-only watchers")
    s.add_argument("--send-existing", action="store_true", help="send already existing comments too; default seeds existing comments silently on first observation")

    s = sub.add_parser("sync-comments-watch", help="poll Dragonfly comments and mirror new ones forever")
    s.add_argument("--count", type=int, default=20, help="recent Dragonfly posts to inspect per tick")
    s.add_argument("--interval", type=float, default=30.0, help="seconds between comment sync ticks")
    s.add_argument("--offset", type=int, default=0, help="Dragonfly feed offset; useful for sharding read-only watchers")
    s.add_argument("--send-existing", action="store_true", help="send already existing comments too; default seeds existing comments silently on first observation")

    s = sub.add_parser("sync-best", help="one-shot forward posts that reached the likes threshold to the best channel")
    s.add_argument("--count", type=int, default=50, help="recent Dragonfly posts to inspect")
    s.add_argument("--offset", type=int, default=0, help="Dragonfly feed offset")
    s.add_argument("--threshold", type=int, default=7, help="minimum likes required for best channel")

    s = sub.add_parser("sync-best-watch", help="poll likes and forward newly qualifying posts to the best channel forever")
    s.add_argument("--count", type=int, default=50, help="recent Dragonfly posts to inspect per tick")
    s.add_argument("--interval", type=float, default=30.0, help="seconds between best sync ticks")
    s.add_argument("--offset", type=int, default=0, help="Dragonfly feed offset")
    s.add_argument("--threshold", type=int, default=7, help="minimum likes required for best channel")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_env_file(args.env_file)
    setup_logging(args.log_file or None, verbose=bool(args.verbose))
    cfg = Config(
        dragonfly_token=optional_env("DRAGONFLY_ACCESS_TOKEN"),
        dragonfly_user_id=args.dragonfly_user_id or optional_env("DRAGONFLY_USER_ID"),
        telegram_token=optional_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=optional_env("TELEGRAM_CHAT_ID") or "@dragonfly_flash",
        db_path=Path(args.db),
        dry_run=bool(args.dry_run),
        feed_type=args.feed_type,
        request_delay=float(args.request_delay),
        send_delay=float(args.send_delay),
        text_delay=float(args.text_delay),
        photo_delay=float(args.photo_delay),
        album_delay=float(args.album_delay),
        animation_delay=float(args.animation_delay),
        mixed_media_delay=float(args.mixed_media_delay),
        media_item_delay=float(args.media_item_delay),
        poll_interval=float(args.poll_interval),
        limit=int(args.limit),
        max_attempts=int(args.max_attempts),
        keep_sent=int(args.keep_sent),
        log_file=args.log_file or None,
        upload_media=not bool(args.url_media),
        alert_chat_id=args.alert_chat_id or optional_env("TELEGRAM_ALERT_CHAT_ID"),
        discussion_chat_id=args.discussion_chat_id or optional_env("TELEGRAM_DISCUSSION_CHAT_ID"),
        best_chat_id=args.best_chat_id or optional_env("TELEGRAM_BEST_CHAT_ID"),
        cookie_file=args.cookie_file or optional_env("DRAGONFLY_COOKIE_FILE"),
        accounts_file=args.accounts_file or optional_env("DRAGONFLY_ACCOUNTS_FILE"),
        account_name=args.dragonfly_account or None,
    )
    configure_active_account(cfg)
    if not cfg.cookie_file and not cfg.dragonfly_token:
        raise SystemExit("Set DRAGONFLY_COOKIE_FILE, DRAGONFLY_ACCESS_TOKEN, or DRAGONFLY_ACCOUNTS_FILE")
    if not cfg.dry_run and (not cfg.telegram_token or not cfg.telegram_chat_id):
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required unless --dry-run")
    con = init_db(cfg.db_path)
    if args.cmd == "auth-check":
        return cmd_auth_check(cfg)
    if args.cmd == "init":
        return cmd_init(cfg, con, args)
    if args.cmd == "backfill":
        return cmd_backfill(cfg, con, args)
    if args.cmd == "watch":
        return cmd_watch(cfg, con, args)
    if args.cmd == "sync-stats":
        return cmd_sync_stats(cfg, con, args)
    if args.cmd == "sync-stats-watch":
        return cmd_sync_stats_watch(cfg, con, args)
    if args.cmd == "sync-comments":
        return cmd_sync_comments(cfg, con, args)
    if args.cmd == "sync-comments-watch":
        return cmd_sync_comments_watch(cfg, con, args)
    if args.cmd == "sync-best":
        return cmd_sync_best(cfg, con, args)
    if args.cmd == "sync-best-watch":
        return cmd_sync_best_watch(cfg, con, args)
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
