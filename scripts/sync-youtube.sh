#!/usr/bin/env bash
# Manually trigger YouTube channel sync
set -euo pipefail

APP_ROOT="/opt/twitch247"
export TWITCH247_CONFIG="${APP_ROOT}/config/config.env"

cd "$APP_ROOT"
exec "${APP_ROOT}/venv/bin/python" -c "
from app.config import load_config
from app.database import Database
from app.youtube_sync import YouTubeSync
from pathlib import Path

config = load_config()
schema = config.app_root / 'database' / 'schema.sql'
db = Database(config.db_path, schema)
sync = YouTubeSync(config.youtube_channel_url, db)
count = sync.sync()
print(f'Sync complete: {count} new videos')
"
