#!/usr/bin/env python3
"""Upload audio tracks to Dragonfly Flash.

Endpoint discovered from the browser app:
  POST https://dragonfly-flash.ru/api/audio/upload
  multipart/form-data fields: artist, title, file

The script is server-friendly: reads secrets from env/.env, supports the same
Dragonfly account pool JSON as dragonfly_telegram_poster.py, retries transient
failures and Telegram-alerts operational problems when configured.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BASE_URL = "https://dragonfly-flash.ru"
UPLOAD_URL = BASE_URL + "/api/audio/upload"
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_ACCOUNTS_FILE = "/home/wacotal/.hermes/state/dragonfly_accounts.json"
DEFAULT_ENV_FILE = "/home/wacotal/dragonfly.env"
TG_API = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class Config:
    access_token: str | None
    accounts_file: str | None
    active_account: str | None
    telegram_token: str | None
    alert_chat_id: str | None
    dry_run: bool
    delay: float
    jitter: float
    retries: int
    max_bytes: int
    timeout: int


class DragonflyAuthExpired(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env_file(path: str | None) -> None:
    if not path:
        return
    p = Path(path).expanduser()
    if not p.exists():
        raise SystemExit(f"Env file not found: {p}")
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def optional_env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def load_accounts(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_accounts(path: str, data: dict[str, Any]) -> None:
    p = Path(path).expanduser()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        p.chmod(0o600)
    except Exception:
        pass


def enabled_accounts(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in data.get("accounts", []) if isinstance(a, dict) and a.get("enabled", True) and a.get("access_token")]


def configure_active_account(cfg: Config) -> None:
    data = load_accounts(cfg.accounts_file)
    if not data:
        return
    accounts = enabled_accounts(data)
    if not accounts:
        return
    active = data.get("active")
    account = next((a for a in accounts if a.get("name") == active), accounts[0])
    cfg.access_token = str(account["access_token"])
    cfg.active_account = str(account.get("name") or account.get("sub") or "account")


def switch_account(cfg: Config, reason: str) -> bool:
    if not cfg.accounts_file:
        return False
    data = load_accounts(cfg.accounts_file)
    if not data:
        return False
    accounts = enabled_accounts(data)
    if len(accounts) <= 1:
        return False
    names = [str(a.get("name") or a.get("sub") or i) for i, a in enumerate(accounts)]
    current = cfg.active_account or data.get("active")
    now = datetime.now(timezone.utc).isoformat()
    for a in data.get("accounts", []):
        if isinstance(a, dict) and a.get("name") == current:
            a["last_error"] = reason[:300]
            a["last_failed_at"] = now
    try:
        start = names.index(str(current)) + 1
    except ValueError:
        start = 0
    for i in range(len(accounts)):
        cand = accounts[(start + i) % len(accounts)]
        name = str(cand.get("name") or cand.get("sub") or "account")
        if name == current:
            continue
        data["active"] = name
        save_accounts(cfg.accounts_file, data)
        old = cfg.active_account or "unknown"
        cfg.active_account = name
        cfg.access_token = str(cand["access_token"])
        msg = f"Dragonfly audio upload: {old} получил auth-ошибку, переключился на {name}. Причина: {reason[:250]}"
        log(msg)
        send_alert(cfg, "Dragonfly uploader переключил аккаунт", msg, level="warning")
        return True
    return False


def jwt_exp(token: str | None) -> str:
    if not token or "." not in token:
        return "unknown"
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part.encode()).decode())
        exp = int(payload.get("exp"))
        return datetime.fromtimestamp(exp, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return "unknown"


def send_alert(cfg: Config, title: str, body: str, *, level: str = "warning") -> None:
    if not cfg.telegram_token or not cfg.alert_chat_id:
        return
    icon = {"info": "ℹ️", "warning": "⚠️", "error": "🚨", "success": "✅"}.get(level, "⚠️")
    text = f"{icon} <b>{html_escape(title)}</b>\n\n{html_escape(body)}"
    try:
        data = urllib.parse.urlencode({
            "chat_id": cfg.alert_chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(TG_API.format(token=cfg.telegram_token, method="sendMessage"), data=data, timeout=20).read()
    except Exception as e:
        log(f"alert failed: {e}")


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def iter_files(inputs: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in inputs:
        p = Path(item).expanduser()
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if child.is_file() and child.suffix.lower() in {".mp3", ".m4a", ".ogg", ".wav", ".flac", ".aac"}:
                    out.append(child)
        else:
            # Expand glob-like arguments ourselves if the shell did not.
            matches = sorted(Path().glob(item)) if any(ch in item for ch in "*?[") else []
            if matches:
                out.extend([m for m in matches if m.is_file()])
            elif p.is_file():
                out.append(p)
            else:
                raise SystemExit(f"File not found: {item}")
    # de-dupe, preserve order
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(rp)
    return uniq


def guess_artist_title(path: Path, default_artist: str | None = None, default_title: str | None = None) -> tuple[str, str]:
    stem = path.stem.strip()
    # Common shape: Artist - Title.mp3 / Artist — Title.mp3
    parts = re.split(r"\s+[-–—]\s+", stem, maxsplit=1)
    if len(parts) == 2:
        artist, title = parts[0].strip(), parts[1].strip()
    else:
        artist, title = default_artist or "Unknown Artist", default_title or stem
    return default_artist or artist or "Unknown Artist", default_title or title or stem


def multipart_body(fields: dict[str, str], file_field: str, file_path: Path, content_type: str) -> tuple[bytes, str]:
    boundary = "----dragonfly-uploader-" + base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    chunks: list[bytes] = []
    for k, v in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        chunks.append(str(v).encode("utf-8"))
        chunks.append(b"\r\n")
    filename = file_path.name.replace('"', "_")
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode())
    chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def upload_one(cfg: Config, path: Path, artist: str, title: str) -> dict[str, Any]:
    size = path.stat().st_size
    if size > cfg.max_bytes:
        raise RuntimeError(f"file too large: {path.name} {size} bytes > {cfg.max_bytes}")
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body, boundary = multipart_body({"artist": artist, "title": title}, "file", path, ctype)
    headers = {
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/",
        "User-Agent": "dragonfly-audio-uploader/1.0",
    }
    if cfg.access_token:
        # Browser uses access_token cookie. Bearer also works for API, but cookie
        # mirrors the site's own upload request most closely.
        headers["Cookie"] = f"access_token={cfg.access_token}"
    req = urllib.request.Request(UPLOAD_URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def upload_with_retries(cfg: Config, path: Path, artist: str, title: str) -> dict[str, Any]:
    attempts_401 = 0
    max_auth_attempts = 1
    data = load_accounts(cfg.accounts_file)
    if data:
        max_auth_attempts = max(1, len(enabled_accounts(data)))
    last: Exception | None = None
    for attempt in range(cfg.retries + 1):
        try:
            return upload_one(cfg, path, artist, title)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                attempts_401 += 1
                reason = f"HTTP 401: {body[:300]}"
                if attempts_401 < max_auth_attempts and switch_account(cfg, reason):
                    continue
                raise DragonflyAuthExpired(reason) from e
            if e.code == 429 and attempt < cfg.retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                sleep_for = min(900.0, 30.0 * (2 ** attempt))
                if retry_after:
                    try:
                        sleep_for = max(sleep_for, float(retry_after))
                    except Exception:
                        pass
                log(f"429 on {path.name}; sleeping {sleep_for:.0f}s before retry {attempt + 1}/{cfg.retries}")
                send_alert(cfg, "Dragonfly uploader получил 429", f"Файл {path.name}: жду {sleep_for:.0f} сек и повторяю попытку {attempt + 1}/{cfg.retries}.")
                time.sleep(sleep_for)
                continue
            if e.code in (500, 502, 503, 504) and attempt < cfg.retries:
                last = RuntimeError(f"HTTP {e.code}: {body[:300]}")
            else:
                raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from e
        except urllib.error.URLError as e:
            last = e
            if attempt >= cfg.retries:
                raise RuntimeError(f"network error: {e}") from e
        sleep_for = min(60.0, 5.0 * (2 ** attempt))
        log(f"retry {path.name}: sleep={sleep_for:.0f}s attempt={attempt + 1}/{cfg.retries} error={last}")
        time.sleep(sleep_for)
    raise RuntimeError(f"upload failed: {last}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload audio tracks to Dragonfly Flash")
    ap.add_argument("files", nargs="+", help="audio files, globs, or directories")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--accounts-file", default=None, help="defaults to env DRAGONFLY_ACCOUNTS_FILE, then known local account pool")
    ap.add_argument("--artist", default=None, help="force artist for all files; otherwise parsed from 'Artist - Title.ext'")
    ap.add_argument("--title", default=None, help="force title; only sensible for one file")
    ap.add_argument("--delay", type=float, default=5.0, help="delay between uploads")
    ap.add_argument("--jitter", type=float, default=1.0, help="random extra delay 0..jitter")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--max-mb", type=float, default=25.0)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_env_file(args.env_file)
    cfg = Config(
        access_token=optional_env("DRAGONFLY_ACCESS_TOKEN"),
        accounts_file=args.accounts_file or optional_env("DRAGONFLY_ACCOUNTS_FILE") or (DEFAULT_ACCOUNTS_FILE if Path(DEFAULT_ACCOUNTS_FILE).exists() else None),
        active_account=None,
        telegram_token=optional_env("TELEGRAM_BOT_TOKEN"),
        alert_chat_id=optional_env("TELEGRAM_ALERT_CHAT_ID"),
        dry_run=bool(args.dry_run),
        delay=float(args.delay),
        jitter=float(args.jitter),
        retries=int(args.retries),
        max_bytes=int(float(args.max_mb) * 1024 * 1024),
        timeout=int(args.timeout),
    )
    configure_active_account(cfg)
    if not cfg.access_token:
        raise SystemExit("Set DRAGONFLY_ACCESS_TOKEN or DRAGONFLY_ACCOUNTS_FILE")
    files = iter_files(args.files)
    if not files:
        raise SystemExit("No audio files found")
    log(f"files={len(files)} account={cfg.active_account or 'single-token'} token_exp={jwt_exp(cfg.access_token)} dry_run={cfg.dry_run}")
    ok = 0
    failed = 0
    for idx, path in enumerate(files, start=1):
        artist, title = guess_artist_title(path, args.artist, args.title if len(files) == 1 else None)
        size_mb = path.stat().st_size / (1024 * 1024)
        log(f"[{idx}/{len(files)}] {path.name} artist={artist!r} title={title!r} size={size_mb:.2f}MB")
        if cfg.dry_run:
            continue
        try:
            res = upload_with_retries(cfg, path, artist, title)
            ok += 1
            log(f"uploaded {path.name}: {json.dumps(res, ensure_ascii=False)[:500]}")
        except Exception as e:
            failed += 1
            log(f"FAILED {path.name}: {e}")
            send_alert(cfg, "Dragonfly uploader: файл не загрузился", f"{path.name}: {str(e)[:500]}", level="error")
        if idx < len(files) and cfg.delay > 0:
            sleep_for = cfg.delay + (random.random() * cfg.jitter if cfg.jitter > 0 else 0)
            time.sleep(sleep_for)
    if not cfg.dry_run:
        send_alert(cfg, "Dragonfly uploader завершил загрузку", f"Готово: ok={ok}, failed={failed}, всего={len(files)}", level="success" if failed == 0 else "warning")
    log(f"done ok={ok} failed={failed} total={len(files)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
