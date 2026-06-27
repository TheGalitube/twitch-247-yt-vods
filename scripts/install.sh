#!/usr/bin/env bash
# Twitch247 Installation Script for Debian/Ubuntu
# Run as root: sudo bash scripts/install.sh

set -euo pipefail

APP_ROOT="/opt/twitch247"
APP_USER="twitch247"
APP_GROUP="twitch247"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

same_path() {
    [[ "$(readlink -f "$1")" == "$(readlink -f "$2")" ]]
}

copy_dir_contents() {
    local src="$1"
    local dst="$2"

    mkdir -p "$dst"

    if same_path "$src" "$dst"; then
        warn "Source and destination are the same ($dst), skipping copy."
        return
    fi

    find "$src" -mindepth 1 -maxdepth 1 ! -name '__pycache__' \
        -exec cp -a -t "$dst" {} +
}

copy_file() {
    local src="$1"
    local dst="$2"

    if same_path "$src" "$dst"; then
        warn "Source and destination are the same ($dst), skipping copy."
        return
    fi

    cp -a "$src" "$dst"
}

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (sudo bash scripts/install.sh)"
    exit 1
fi

info "=== Twitch247 Installation ==="

# --- System dependencies ---
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    sqlite3 \
    curl \
    ca-certificates

# Install yt-dlp (prefer pip for latest version)
if ! command -v yt-dlp &>/dev/null; then
    info "Installing yt-dlp..."
    curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp
    chmod a+rx /usr/local/bin/yt-dlp
fi

# --- Service user ---
if ! getent group "$APP_GROUP" &>/dev/null; then
    info "Creating system group: $APP_GROUP"
    groupadd --system "$APP_GROUP"
fi

if ! id "$APP_USER" &>/dev/null; then
    info "Creating system user: $APP_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --gid "$APP_GROUP" "$APP_USER"
fi

# --- Directory structure ---
info "Creating directory structure at $APP_ROOT..."
mkdir -p "$APP_ROOT"/{app,database,logs,config,services,dashboard,scripts}

# --- Copy application files ---
info "Copying application files..."
copy_dir_contents "${SOURCE_DIR}/app"       "$APP_ROOT/app"
copy_dir_contents "${SOURCE_DIR}/dashboard" "$APP_ROOT/dashboard"
copy_file "${SOURCE_DIR}/database/schema.sql" "$APP_ROOT/database/schema.sql"

if same_path "${SOURCE_DIR}/scripts" "$APP_ROOT/scripts"; then
    warn "Source and destination are the same ($APP_ROOT/scripts), skipping copy."
else
    cp -a "${SOURCE_DIR}/scripts/"*.sh "$APP_ROOT/scripts/"
fi

copy_file "${SOURCE_DIR}/requirements.txt" "$APP_ROOT/requirements.txt"
chmod +x "$APP_ROOT/scripts/"*.sh

# --- Configuration ---
if [[ ! -f "$APP_ROOT/config/config.env" ]]; then
    cp "${SOURCE_DIR}/config/config.env.example" "$APP_ROOT/config/config.env"
    warn "Config created at $APP_ROOT/config/config.env — edit TWITCH_STREAM_KEY before starting!"
else
    info "Config already exists, skipping."
fi

# --- Python virtual environment ---
info "Setting up Python virtual environment..."
python3 -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/pip" install --upgrade pip -q
"$APP_ROOT/venv/bin/pip" install -r "$APP_ROOT/requirements.txt" -q

# --- Permissions needed before database initialization ---
info "Setting permissions..."
chown -R "$APP_USER:$APP_GROUP" "$APP_ROOT"
chmod 750 "$APP_ROOT"
chmod 640 "$APP_ROOT/config/config.env"
chmod 770 "$APP_ROOT/logs" "$APP_ROOT/database"

# --- Initialize database ---
info "Initializing SQLite database..."
sudo -u "$APP_USER" env HOME=/tmp \
    "$APP_ROOT/venv/bin/python" -c "
import sys
sys.path.insert(0, '$APP_ROOT')
from app.database import Database
from pathlib import Path
db = Database(
    Path('$APP_ROOT/database/twitch247.db'),
    Path('$APP_ROOT/database/schema.sql'),
)
print('Database initialized.')
"

# Re-apply final ownership and permissions after SQLite creates WAL/SHM files.
chown -R "$APP_USER:$APP_GROUP" "$APP_ROOT"
chmod 750 "$APP_ROOT"
chmod 640 "$APP_ROOT/config/config.env"
chmod 770 "$APP_ROOT/logs" "$APP_ROOT/database"

# --- Systemd services ---
info "Installing systemd services..."
cp "${SOURCE_DIR}/services/twitch247.service"              /etc/systemd/system/
cp "${SOURCE_DIR}/services/twitch247-dashboard.service"    /etc/systemd/system/
cp "${SOURCE_DIR}/services/twitch247-watchdog.service"     /etc/systemd/system/
cp "${SOURCE_DIR}/services/twitch247-watchdog.timer"       /etc/systemd/system/

systemctl daemon-reload
systemctl enable twitch247.service
systemctl enable twitch247-dashboard.service
systemctl enable twitch247-watchdog.timer

info ""
info "=== Installation Complete ==="
info ""
info "Next steps:"
info "  1. Edit config:  nano $APP_ROOT/config/config.env"
info "  2. Set TWITCH_STREAM_KEY to your Twitch stream key"
info "  3. Start services:"
info "       systemctl start twitch247"
info "       systemctl start twitch247-dashboard"
info "       systemctl start twitch247-watchdog.timer"
info "  4. Monitor:      journalctl -u twitch247 -f"
info "  5. Dashboard:    http://YOUR_SERVER:8080"
info ""
warn "Make sure port 8080 is accessible if you want remote dashboard access."
