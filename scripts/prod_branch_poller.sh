#!/usr/bin/env bash
set -Eeuo pipefail

# Default deploy target is origin/prod; override with PM2_DEPLOY_REMOTE / PM2_DEPLOY_BRANCH.
BRANCH="${PM2_DEPLOY_BRANCH:-prod}"
REMOTE="${PM2_DEPLOY_REMOTE:-origin}"
REMOTE_REF="${REMOTE}/${BRANCH}"
INTERVAL="${PM2_DEPLOY_INTERVAL:-60}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DEPLOY_SCRIPT="$REPO_DIR/scripts/deploy_prod.sh"

cd "$REPO_DIR"

echo "prod poller: watching $REMOTE_REF every ${INTERVAL}s"
last_seen=""
while true; do
  if git fetch "$REMOTE" "$BRANCH" >/tmp/dragonfly-prod-poller-fetch.log 2>&1; then
    remote_sha="$(git rev-parse "$REMOTE_REF")"
    local_sha="$(git rev-parse HEAD)"
    if [[ "$remote_sha" != "$local_sha" && "$remote_sha" != "$last_seen" ]]; then
      echo "prod poller: deploying $remote_sha"
      if "$DEPLOY_SCRIPT"; then
        last_seen="$remote_sha"
      else
        echo "prod poller: deploy failed for $remote_sha; will retry" >&2
      fi
    fi
  else
    echo "prod poller: git fetch failed" >&2
    cat /tmp/dragonfly-prod-poller-fetch.log >&2 || true
  fi
  sleep "$INTERVAL"
done
