#!/usr/bin/env bash
set -Eeuo pipefail

# Default deploy target is origin/prod; override with PM2_DEPLOY_REMOTE / PM2_DEPLOY_BRANCH.
BRANCH="${PM2_DEPLOY_BRANCH:-prod}"
REMOTE="${PM2_DEPLOY_REMOTE:-origin}"
REMOTE_REF="${REMOTE}/${BRANCH}"
ENV_FILE="${DRAGONFLY_ENV_FILE:-$HOME/dragonfly/dragonfly.env}"
STATE_DIR="${DRAGONFLY_STATE_DIR:-$HOME/dragonfly/state}"
LOG_DIR="${DRAGONFLY_LOG_DIR:-$HOME/dragonfly/logs}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

cd "$REPO_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 2
fi
if [[ ! -f ecosystem.config.cjs ]]; then
  echo "missing ecosystem.config.cjs in $REPO_DIR" >&2
  exit 2
fi
if ! command -v pm2 >/dev/null 2>&1; then
  echo "pm2 is required: install with npm install -g pm2" >&2
  exit 2
fi
mkdir -p "$STATE_DIR" "$LOG_DIR"

echo "deploy: fetching $REMOTE_REF"
git fetch "$REMOTE" "$BRANCH"
git reset --hard "$REMOTE_REF"

echo "deploy: validating python syntax"
python3 -m py_compile dragonfly_telegram_poster.py dragonfly_audio_uploader.py scripts/install_systemd_user.py

echo "deploy: running local doctor"
python3 dragonfly_telegram_poster.py --env-file "$ENV_FILE" --db "$STATE_DIR/dragonfly_telegram_poster.sqlite3" --log-file "$LOG_DIR/dragonfly_telegram_poster.log" doctor --no-network

echo "deploy: starting/reloading PM2 apps"
DRAGONFLY_ENV_FILE="$ENV_FILE" DRAGONFLY_STATE_DIR="$STATE_DIR" DRAGONFLY_LOG_DIR="$LOG_DIR" pm2 startOrReload ecosystem.config.cjs --update-env
pm2 save
pm2 status
