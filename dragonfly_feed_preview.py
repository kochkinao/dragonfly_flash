#!/usr/bin/env python3
"""Preview/prepare Dragonfly Flash feed posts for Telegram.

Dry-run prototype: fetches latest feed items, tracks seen post_id in a JSON state
file, and prints Telegram-ready payloads instead of sending them.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_URL = "https://dragonfly-flash.ru"
FEED_URL = BASE_URL + "/api/feed?type={type}&limit={limit}&offset=0"
DEFAULT_STATE = Path.home() / ".hermes" / "state" / "dragonfly_feed_seen.json"


def require_token() -> str:
    token = os.environ.get("DRAGONFLY_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set DRAGONFLY_ACCESS_TOKEN env var")
    return token


def api_get(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "dragonfly-telegram-feed/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Dragonfly API HTTP {e.code}: {body[:500]}")


def abs_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return BASE_URL + "/" + path.lstrip("/")


def audio_url(file_path: str | None) -> str | None:
    if not file_path:
        return None
    if file_path.startswith("http://") or file_path.startswith("https://"):
        return file_path
    # Confirmed from site JS: getSource(path) => '/audio/' + encoded path parts
    parts = [urllib_quote(part) for part in file_path.lstrip("/").split("/")]
    return BASE_URL + "/audio/" + "/".join(parts)


def urllib_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def parse_dt(s: str | None) -> str:
    if not s:
        return ""
    try:
        # API returns naive UTC-ish timestamps; frontend appends Z.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return s


def format_post_html(post: dict[str, Any]) -> str:
    pid = post.get("post_id")
    author = html.escape(str(post.get("author_name") or "Пользователь"))
    created = html.escape(parse_dt(post.get("created_at")))
    text = html.escape((post.get("description") or "").strip())
    post_url = f"{BASE_URL}/?post={pid}"

    lines = [f"<b>{author}</b>"]
    if created:
        lines.append(f"<i>{created}</i>")
    if text:
        lines.append("")
        lines.append(text)

    photos = post.get("photos") or []
    audios = post.get("audios") or []
    if photos or audios or post.get("is_repost"):
        parts = []
        if photos:
            parts.append(f"📷 {len(photos)}")
        if audios:
            parts.append(f"🎵 {len(audios)}")
        if post.get("is_repost"):
            parts.append("🔄 репост")
        lines.append("")
        lines.append(" ".join(parts))

    lines.append("")
    lines.append(f'<a href="{html.escape(post_url)}">Открыть пост #{pid}</a>')
    return "\n".join(lines)


def payload_for_post(post: dict[str, Any]) -> dict[str, Any]:
    photos = [abs_url(p.get("url")) for p in (post.get("photos") or []) if p.get("url")]
    audios = [
        {
            "id": a.get("id"),
            "artist": a.get("artist"),
            "title": a.get("title"),
            "url": audio_url(a.get("file_path")),
        }
        for a in (post.get("audios") or [])
    ]
    return {
        "post_id": post.get("post_id"),
        "html": format_post_html(post),
        "photos": photos,
        "audios": audios,
        "original": f"{BASE_URL}/?post={post.get('post_id')}",
    }


def load_seen(path: Path) -> set[int]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(x) for x in data.get("seen_post_ids", [])}
    except Exception:
        return set()


def save_seen(path: Path, seen: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # cap to recent ids to avoid unbounded growth
    ids = sorted(seen, reverse=True)[:5000]
    path.write_text(json.dumps({"seen_post_ids": ids}, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="all", choices=["all", "friends"])
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--init", action="store_true", help="mark current feed as seen without printing")
    ap.add_argument("--no-state", action="store_true", help="ignore/update no state; print fetched posts")
    args = ap.parse_args()

    token = require_token()
    data = api_get(FEED_URL.format(type=args.type, limit=args.limit), token)
    feed = data.get("feed") or []
    posts = [p for p in feed if isinstance(p.get("post_id"), int)]
    posts.sort(key=lambda p: p["post_id"])

    state_path = Path(args.state)
    seen = set() if args.no_state else load_seen(state_path)

    if args.init:
        seen.update(p["post_id"] for p in posts)
        save_seen(state_path, seen)
        print(f"Initialized state with {len(posts)} current posts. Max post_id={max(seen) if seen else 'none'}")
        return 0

    new_posts = posts if args.no_state else [p for p in posts if p["post_id"] not in seen]
    for p in new_posts:
        print(json.dumps(payload_for_post(p), ensure_ascii=False, indent=2))
        print("---")
        seen.add(p["post_id"])

    if not args.no_state:
        save_seen(state_path, seen)
    print(f"Fetched={len(posts)} New={len(new_posts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
