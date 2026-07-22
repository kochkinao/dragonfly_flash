#!/usr/bin/env python3
"""Render/install user systemd units for the Dragonfly Telegram bridge.

This script is intentionally portable: templates in deploy/systemd/user use
{{PROJECT_DIR}} and {{ENV_FILE}} placeholders, and this installer renders them
for the current server. It does not require root when used with --user units.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "deploy" / "systemd" / "user"
DEFAULT_OUTPUT_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_GLOB = "dragonfly-*.service"
TARGET_GLOB = "dragonfly-*.target"


def render_template(text: str, *, project_dir: Path, env_file: Path) -> str:
    rendered = text.replace("{{PROJECT_DIR}}", str(project_dir)).replace("{{ENV_FILE}}", str(env_file))
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("unrendered placeholder left in systemd unit")
    return rendered


def render_units(*, project_dir: Path, env_file: Path, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered_paths: list[Path] = []
    templates = sorted(TEMPLATE_DIR.glob(UNIT_GLOB)) + sorted(TEMPLATE_DIR.glob(TARGET_GLOB))
    if not templates:
        raise FileNotFoundError(f"no systemd templates found in {TEMPLATE_DIR}")
    for template in templates:
        rendered = render_template(template.read_text(encoding="utf-8"), project_dir=project_dir, env_file=env_file)
        dest = output_dir / template.name
        dest.write_text(rendered, encoding="utf-8")
        rendered_paths.append(dest)
    return rendered_paths


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Dragonfly bridge user systemd units")
    parser.add_argument("--project-dir", type=Path, default=ROOT, help="repo checkout path on this server")
    parser.add_argument("--env-file", type=Path, default=Path.home() / "dragonfly.env", help="runtime env file with secrets")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="systemd user unit directory")
    parser.add_argument("--enable", action="store_true", help="enable dragonfly-bridge.target for user login")
    parser.add_argument("--start", action="store_true", help="start dragonfly-bridge.target after rendering")
    parser.add_argument("--no-daemon-reload", action="store_true", help="skip systemctl --user daemon-reload")
    args = parser.parse_args()

    project_dir = args.project_dir.expanduser().resolve()
    env_file = args.env_file.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not (project_dir / "dragonfly_telegram_poster.py").exists():
        raise SystemExit(f"project dir does not contain dragonfly_telegram_poster.py: {project_dir}")
    if not env_file.exists():
        raise SystemExit(f"env file does not exist: {env_file}")

    rendered = render_units(project_dir=project_dir, env_file=env_file, output_dir=output_dir)
    for path in rendered:
        print(path)

    if not args.no_daemon_reload:
        run(["systemctl", "--user", "daemon-reload"])
    if args.enable:
        run(["systemctl", "--user", "enable", "dragonfly-bridge.target"])
    if args.start:
        run(["systemctl", "--user", "start", "dragonfly-bridge.target"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
