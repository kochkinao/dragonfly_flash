#!/usr/bin/env bash
set -Eeuo pipefail

pm2 status
pm2 logs --nostream --lines "${PM2_STATUS_LINES:-80}" dragonfly-watch dragonfly-feed-cache dragonfly-stats-hot dragonfly-stats-cold dragonfly-comments-0 dragonfly-comments-17 dragonfly-comments-34
