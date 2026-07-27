#!/usr/bin/env bash
# Twitch247 Installation Script for Debian/Ubuntu
# Run as root: sudo bash scripts/install.sh

set -euo pipefail

APP_ROOT="/opt/twitch247"
APP_USER="twitch247"
APP_GROUP="twitch247"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_ID="$(date -u +%Y%m%d%H%M%S)-$$"
VENV_RELEASE_ROOT="${APP_ROOT}/.venv-releases"

WORK_DIR=""
STAGED_SOURCE=""
BACKUP_ROOT=""
VENV_NEW=""
VENV_LEGACY=""
VENV_OLD_RELEASE=""
VENV_PREVIOUS_LINK=""
VENV_LINK_TEMP="${APP_ROOT}/.venv-link-${INSTALL_ID}"

STREAMER_WAS_ACTIVE=0
STREAMER_WAS_PUBLISHING=0
DASHBOARD_WAS_ACTIVE=0
WATCHDOG_TIMER_WAS_ACTIVE=0
STREAMER_WAS_ENABLED=0
DASHBOARD_WAS_ENABLED=0
WATCHDOG_TIMER_WAS_ENABLED=0
MAINTENANCE_ACTIVE=0
DEPLOYMENT_STARTED=0
VENV_SWITCHED=0

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

safe_remove_tree() {
    local target="$1"

    [[ -n "$target" ]] || return 0
    case "$target" in
        "$WORK_DIR"|"$VENV_NEW"|"$VENV_LEGACY"|"$VENV_OLD_RELEASE")
            rm -rf -- "$target"
            ;;
        *)
            error "Refusing to remove unexpected install path: $target"
            return 1
            ;;
    esac
}

clear_deploy_dir() {
    local target="$1"

    case "$target" in
        "$APP_ROOT/app"|"$APP_ROOT/dashboard"|"$APP_ROOT/scripts"|\
        "$APP_ROOT/services"|"$APP_ROOT/config"|"$APP_ROOT/database")
            mkdir -p "$target"
            find "$target" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
            ;;
        *)
            error "Refusing to clear unexpected deployment directory: $target"
            return 1
            ;;
    esac
}

replace_dir_contents() {
    local source="$1"
    local target="$2"

    clear_deploy_dir "$target"
    find "$source" -mindepth 1 -maxdepth 1 ! -name '__pycache__' \
        -exec cp -a -t "$target" {} +
}

unit_was_enabled() {
    local unit="$1"
    systemctl is-enabled --quiet "$unit" 2>/dev/null
}

stop_unit_verified() {
    local unit="$1"

    systemctl stop "$unit" 2>/dev/null || true
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        error "Could not stop ${unit}; refusing to modify the live installation."
        return 1
    fi
}

stop_runtime_verified() {
    stop_unit_verified twitch247-watchdog.timer
    stop_unit_verified twitch247-watchdog.service
    stop_unit_verified twitch247.service
    stop_unit_verified twitch247-dashboard.service
}

stop_runtime_best_effort() {
    systemctl stop twitch247-watchdog.timer >/dev/null 2>&1 || true
    systemctl stop twitch247-watchdog.service >/dev/null 2>&1 || true
    systemctl stop twitch247.service >/dev/null 2>&1 || true
    systemctl stop twitch247-dashboard.service >/dev/null 2>&1 || true
}

restore_enablement_best_effort() {
    local unit enabled

    for unit in \
        twitch247.service \
        twitch247-dashboard.service \
        twitch247-watchdog.timer; do
        case "$unit" in
            twitch247.service) enabled="$STREAMER_WAS_ENABLED" ;;
            twitch247-dashboard.service) enabled="$DASHBOARD_WAS_ENABLED" ;;
            twitch247-watchdog.timer) enabled="$WATCHDOG_TIMER_WAS_ENABLED" ;;
        esac

        if (( enabled == 1 )); then
            systemctl enable "$unit" >/dev/null 2>&1 || true
        else
            systemctl disable "$unit" >/dev/null 2>&1 || true
        fi
    done
}

resume_previous_services_best_effort() {
    systemctl daemon-reload >/dev/null 2>&1 || true
    restore_enablement_best_effort

    if (( STREAMER_WAS_ACTIVE == 1 )); then
        systemctl restart twitch247.service >/dev/null 2>&1 || true
    fi
    if (( DASHBOARD_WAS_ACTIVE == 1 )); then
        systemctl restart twitch247-dashboard.service >/dev/null 2>&1 || true
    fi
    if (( WATCHDOG_TIMER_WAS_ACTIVE == 1 )); then
        systemctl restart twitch247-watchdog.timer >/dev/null 2>&1 || true
    fi
}

restore_venv_best_effort() {
    local restored=0

    (( VENV_SWITCHED == 1 )) || return 0

    rm -f -- "$VENV_LINK_TEMP"
    if [[ -n "$VENV_PREVIOUS_LINK" ]]; then
        ln -s "$VENV_PREVIOUS_LINK" "$VENV_LINK_TEMP" 2>/dev/null || true
        mv -Tf "$VENV_LINK_TEMP" "$APP_ROOT/venv" 2>/dev/null || true
        if [[ -L "$APP_ROOT/venv" ]] \
            && [[ "$(readlink "$APP_ROOT/venv" 2>/dev/null || true)" \
                == "$VENV_PREVIOUS_LINK" ]]; then
            restored=1
        fi
    elif [[ -n "$VENV_LEGACY" && -d "$VENV_LEGACY" ]]; then
        rm -f -- "$APP_ROOT/venv"
        mv "$VENV_LEGACY" "$APP_ROOT/venv" 2>/dev/null || true
        if [[ -d "$APP_ROOT/venv" && ! -L "$APP_ROOT/venv" ]] \
            && [[ ! -e "$VENV_LEGACY" ]]; then
            restored=1
        fi
    else
        rm -f -- "$APP_ROOT/venv"
        if [[ ! -e "$APP_ROOT/venv" && ! -L "$APP_ROOT/venv" ]]; then
            restored=1
        fi
    fi

    if (( restored == 1 )) && [[ -n "$VENV_NEW" && -d "$VENV_NEW" ]]; then
        safe_remove_tree "$VENV_NEW" 2>/dev/null || true
        VENV_SWITCHED=0
    elif (( restored == 0 )); then
        error "Could not restore the previous virtual environment selector; keeping the new release as a runnable fallback."
        if [[ ! -x "$APP_ROOT/venv/bin/python" \
            && -x "$VENV_NEW/bin/python" \
            && ! -e "$APP_ROOT/venv" \
            && ! -L "$APP_ROOT/venv" ]]; then
            ln -s "$VENV_NEW" "$APP_ROOT/venv" 2>/dev/null || true
        fi
    fi
}

backup_deployment() {
    local unit

    BACKUP_ROOT="${WORK_DIR}/backup"
    mkdir -p "$BACKUP_ROOT/app-root" "$BACKUP_ROOT/systemd"

    for unit in app dashboard scripts services config database; do
        cp -a "$APP_ROOT/$unit" "$BACKUP_ROOT/app-root/$unit"
    done

    if [[ -f "$APP_ROOT/requirements.txt" ]]; then
        cp -a "$APP_ROOT/requirements.txt" "$BACKUP_ROOT/app-root/requirements.txt"
    else
        touch "$BACKUP_ROOT/app-root/.requirements-missing"
    fi

    for unit in \
        twitch247.service \
        twitch247-dashboard.service \
        twitch247-watchdog.service \
        twitch247-watchdog.timer; do
        if [[ -f "/etc/systemd/system/$unit" ]]; then
            cp -a "/etc/systemd/system/$unit" "$BACKUP_ROOT/systemd/$unit"
        else
            touch "$BACKUP_ROOT/systemd/.missing-$unit"
        fi
    done
}

restore_deployment_best_effort() {
    local name unit

    (( DEPLOYMENT_STARTED == 1 )) || return 0
    [[ -n "$BACKUP_ROOT" && -d "$BACKUP_ROOT" ]] || return 0

    for name in app dashboard scripts services config database; do
        clear_deploy_dir "$APP_ROOT/$name" 2>/dev/null || true
        if [[ -d "$BACKUP_ROOT/app-root/$name" ]]; then
            find "$BACKUP_ROOT/app-root/$name" -mindepth 1 -maxdepth 1 \
                -exec cp -a -t "$APP_ROOT/$name" {} + 2>/dev/null || true
        fi
    done

    if [[ -f "$BACKUP_ROOT/app-root/.requirements-missing" ]]; then
        rm -f -- "$APP_ROOT/requirements.txt"
    elif [[ -f "$BACKUP_ROOT/app-root/requirements.txt" ]]; then
        cp -a "$BACKUP_ROOT/app-root/requirements.txt" \
            "$APP_ROOT/requirements.txt" 2>/dev/null || true
    fi

    for unit in \
        twitch247.service \
        twitch247-dashboard.service \
        twitch247-watchdog.service \
        twitch247-watchdog.timer; do
        if [[ -f "$BACKUP_ROOT/systemd/.missing-$unit" ]]; then
            rm -f -- "/etc/systemd/system/$unit"
        elif [[ -f "$BACKUP_ROOT/systemd/$unit" ]]; then
            cp -a "$BACKUP_ROOT/systemd/$unit" \
                "/etc/systemd/system/$unit" 2>/dev/null || true
        fi
    done
}

install_cleanup() {
    local exit_code="$1"
    trap - EXIT

    if (( exit_code != 0 && MAINTENANCE_ACTIVE == 1 )); then
        warn "Installation failed; rolling back code, database, units, and Python environment."
        stop_runtime_best_effort
        restore_deployment_best_effort
        restore_venv_best_effort
        set_app_permissions >/dev/null 2>&1 || true
        resume_previous_services_best_effort
    fi

    if (( exit_code != 0 && VENV_SWITCHED == 0 )) \
        && [[ -n "$VENV_NEW" && -d "$VENV_NEW" ]] \
        && [[ "$(readlink -f "$APP_ROOT/venv" 2>/dev/null || true)" != "$VENV_NEW" ]]; then
        safe_remove_tree "$VENV_NEW" 2>/dev/null || true
    fi

    rm -f -- "$VENV_LINK_TEMP"
    if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
        safe_remove_tree "$WORK_DIR" 2>/dev/null || true
    fi

    exit "$exit_code"
}

secure_in_place_source() {
    local path

    same_path "$SOURCE_DIR" "$APP_ROOT" || return 0

    chown root:root "$APP_ROOT"
    chmod 755 "$APP_ROOT"
    for path in app dashboard scripts services; do
        [[ -e "$SOURCE_DIR/$path" ]] || continue
        chown -R root:root "$SOURCE_DIR/$path"
        chmod -R go-w "$SOURCE_DIR/$path"
    done
    for path in \
        requirements.txt \
        database/schema.sql \
        config/config.env.example; do
        [[ -e "$SOURCE_DIR/$path" ]] || continue
        chown root:root "$SOURCE_DIR/$path"
        chmod go-w "$SOURCE_DIR/$path"
    done
}

set_app_permissions() {
    # Code and dependency inputs consumed by root must not be writable by the
    # service account. Only runtime data belongs to twitch247.
    chown root:root "$APP_ROOT"
    chmod 755 "$APP_ROOT"

    local static_path
    for static_path in \
        "$APP_ROOT/.git" \
        "$APP_ROOT/app" \
        "$APP_ROOT/dashboard" \
        "$APP_ROOT/docs" \
        "$APP_ROOT/services" \
        "$APP_ROOT/scripts"; do
        [[ -e "$static_path" ]] || continue
        chown -R root:root "$static_path"
        chmod -R go-w "$static_path"
    done

    for static_path in \
        "$APP_ROOT/.gitignore" \
        "$APP_ROOT/README.md" \
        "$APP_ROOT/requirements.txt"; do
        [[ -e "$static_path" ]] || continue
        chown root:root "$static_path"
        chmod 644 "$static_path"
    done

    if [[ -d "$VENV_RELEASE_ROOT" ]]; then
        chown -R root:root "$VENV_RELEASE_ROOT"
        chmod -R a+rX,go-w "$VENV_RELEASE_ROOT"
    fi
    if [[ -d "$APP_ROOT/venv" && ! -L "$APP_ROOT/venv" ]]; then
        chown -R root:root "$APP_ROOT/venv"
        chmod -R a+rX,go-w "$APP_ROOT/venv"
    fi

    chown -R "root:$APP_GROUP" "$APP_ROOT/config"
    find "$APP_ROOT/config" -type d -exec chmod 750 {} +
    find "$APP_ROOT/config" -type f -exec chmod 640 {} +
    if [[ -f "$APP_ROOT/config/youtube-cookies.txt" ]]; then
        chown "$APP_USER:$APP_GROUP" "$APP_ROOT/config/youtube-cookies.txt"
        chmod 600 "$APP_ROOT/config/youtube-cookies.txt"
    fi
    if [[ -d "$APP_ROOT/config/google-chrome-twitch247-service" ]]; then
        chown -R "$APP_USER:$APP_GROUP" \
            "$APP_ROOT/config/google-chrome-twitch247-service"
        find "$APP_ROOT/config/google-chrome-twitch247-service" \
            -type d -exec chmod 700 {} +
        find "$APP_ROOT/config/google-chrome-twitch247-service" \
            -type f -exec chmod 600 {} +
    fi

    chown -R "$APP_USER:$APP_GROUP" "$APP_ROOT/logs"
    chmod 770 "$APP_ROOT/logs"
    find "$APP_ROOT/logs" -type f -exec chmod 640 {} +

    # Keep the schema immutable while allowing SQLite runtime files beside it.
    chown "root:$APP_GROUP" "$APP_ROOT/database"
    chmod 1770 "$APP_ROOT/database"
    find "$APP_ROOT/database" -maxdepth 1 -type f ! -name schema.sql \
        -exec chown "$APP_USER:$APP_GROUP" {} + \
        -exec chmod 640 {} +
    if [[ -f "$APP_ROOT/database/schema.sql" ]]; then
        chown "root:$APP_GROUP" "$APP_ROOT/database/schema.sql"
        chmod 640 "$APP_ROOT/database/schema.sql"
    fi

    find "$APP_ROOT/scripts" -maxdepth 1 -type f -name '*.sh' \
        -exec chown root:root {} + -exec chmod 755 {} +
    find "$APP_ROOT/services" -maxdepth 1 -type f \
        \( -name '*.service' -o -name '*.timer' \) \
        -exec chown root:root {} + -exec chmod 644 {} +
}

stage_source() {
    local path

    STAGED_SOURCE="${WORK_DIR}/source"
    mkdir -p \
        "$STAGED_SOURCE/database" \
        "$STAGED_SOURCE/config"

    for path in app dashboard scripts services; do
        [[ -d "$SOURCE_DIR/$path" ]] || {
            error "Required source directory is missing: $SOURCE_DIR/$path"
            return 1
        }
        cp -a "$SOURCE_DIR/$path" "$STAGED_SOURCE/$path"
    done

    for path in \
        requirements.txt \
        database/schema.sql \
        config/config.env.example; do
        [[ -f "$SOURCE_DIR/$path" && ! -L "$SOURCE_DIR/$path" ]] || {
            error "Required source file is missing or unsafe: $SOURCE_DIR/$path"
            return 1
        }
    done

    install -o root -g root -m 0600 \
        "$SOURCE_DIR/requirements.txt" "$STAGED_SOURCE/requirements.txt"
    install -o root -g root -m 0644 \
        "$SOURCE_DIR/database/schema.sql" "$STAGED_SOURCE/database/schema.sql"
    install -o root -g root -m 0640 \
        "$SOURCE_DIR/config/config.env.example" \
        "$STAGED_SOURCE/config/config.env.example"

    if find "$STAGED_SOURCE" -type l -print -quit | grep -q .; then
        error "Refusing to install a source tree containing symbolic links."
        return 1
    fi

    find "$STAGED_SOURCE" -type d -name '__pycache__' -prune \
        -exec rm -rf -- {} +
    chown -R root:root "$STAGED_SOURCE"
    chmod -R go-w "$STAGED_SOURCE"
}

switch_venv() {
    mkdir -p "$VENV_RELEASE_ROOT"

    if [[ -L "$APP_ROOT/venv" ]]; then
        VENV_PREVIOUS_LINK="$(readlink "$APP_ROOT/venv")"
        VENV_OLD_RELEASE="$(readlink -f "$APP_ROOT/venv" 2>/dev/null || true)"
    elif [[ -d "$APP_ROOT/venv" ]]; then
        VENV_LEGACY="${VENV_RELEASE_ROOT}/legacy-${INSTALL_ID}"
        mv "$APP_ROOT/venv" "$VENV_LEGACY"
    elif [[ -e "$APP_ROOT/venv" ]]; then
        error "$APP_ROOT/venv is neither a directory nor a symbolic link."
        return 1
    fi

    VENV_SWITCHED=1
    ln -s "$VENV_NEW" "$VENV_LINK_TEMP"
    mv -Tf "$VENV_LINK_TEMP" "$APP_ROOT/venv"
}

wait_for_unit_stability() {
    local unit="$1"
    local required_seconds="${2:-10}"
    local stable_seconds=0
    local previous_pid=""
    local current_pid

    while (( stable_seconds < required_seconds )); do
        systemctl is-active --quiet "$unit" || return 1
        current_pid="$(systemctl show "$unit" -p MainPID --value)"
        [[ "$current_pid" =~ ^[1-9][0-9]*$ ]] || return 1

        if [[ "$current_pid" == "$previous_pid" ]]; then
            stable_seconds=$((stable_seconds + 1))
        else
            previous_pid="$current_pid"
            stable_seconds=0
        fi
        sleep 1
    done
}

has_healthy_rtmp_socket() {
    local pid sockets
    sockets="$(ss -Htanp state established 2>/dev/null || true)"
    for pid in $(pgrep -u "$APP_USER" -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true); do
        if grep "pid=${pid}," <<< "$sockets" | grep -q ":1935"; then
            return 0
        fi
    done
    return 1
}

wait_for_rtmp_publish() {
    local attempts=0

    while (( attempts < 60 )); do
        systemctl is-active --quiet twitch247.service || return 1
        has_healthy_rtmp_socket && return 0
        sleep 2
        attempts=$((attempts + 1))
    done
    return 1
}

configured_dashboard_port() {
    local port
    port="$(awk '
        /^[[:space:]]*DASHBOARD_PORT[[:space:]]*=/ {
            sub(/^[^=]*=[[:space:]]*/, "")
            sub(/[[:space:]]+$/, "")
            gsub(/^["\047]|["\047]$/, "")
            print
            exit
        }
    ' "$APP_ROOT/config/config.env" 2>/dev/null || true)"
    port="${port:-8080}"

    if [[ "$port" =~ ^[0-9]{1,5}$ ]] \
        && (( 10#$port >= 1 && 10#$port <= 65535 )); then
        printf '%s\n' "$((10#$port))"
    else
        printf '8080\n'
    fi
}

wait_for_dashboard_health() {
    local port attempts=0
    port="$(configured_dashboard_port)"

    while (( attempts < 30 )); do
        systemctl is-active --quiet twitch247-dashboard.service || return 1
        if curl -fsS --connect-timeout 1 --max-time 2 \
            "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        attempts=$((attempts + 1))
    done
    return 1
}

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (sudo bash scripts/install.sh)"
    exit 1
fi

WORK_DIR="$(mktemp -d /var/tmp/twitch247-install.XXXXXX)"
chmod 700 "$WORK_DIR"
trap 'install_cleanup $?' EXIT

info "=== Twitch247 Installation ==="
secure_in_place_source
info "Capturing an immutable source snapshot..."
stage_source

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
    iproute2 \
    procps \
    util-linux \
    ca-certificates

# Install yt-dlp (prefer pip for latest version)
if ! command -v yt-dlp &>/dev/null; then
    info "Installing yt-dlp..."
    curl -fL --connect-timeout 5 --max-time 120 \
        https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
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

# --- Directory structure and secure ownership ---
info "Creating directory structure at $APP_ROOT..."
mkdir -p "$APP_ROOT"/{app,database,logs,config,services,dashboard,scripts}
set_app_permissions

# Build in its permanent versioned path. Python virtual environments are not
# relocatable, so this directory is never moved after creation.
info "Preparing replacement Python virtual environment..."
mkdir -p "$VENV_RELEASE_ROOT"
chown root:root "$VENV_RELEASE_ROOT"
chmod 755 "$VENV_RELEASE_ROOT"
VENV_NEW="${VENV_RELEASE_ROOT}/${INSTALL_ID}"
python3 -m venv "$VENV_NEW"
"$VENV_NEW/bin/pip" install --upgrade pip -q
"$VENV_NEW/bin/pip" install -r "$STAGED_SOURCE/requirements.txt" -q
chown -R root:root "$VENV_NEW"
chmod -R a+rX,go-w "$VENV_NEW"

# Record the exact state to restore before entering maintenance.
systemctl is-active --quiet twitch247.service 2>/dev/null \
    && STREAMER_WAS_ACTIVE=1 || true
systemctl is-active --quiet twitch247-dashboard.service 2>/dev/null \
    && DASHBOARD_WAS_ACTIVE=1 || true
systemctl is-active --quiet twitch247-watchdog.timer 2>/dev/null \
    && WATCHDOG_TIMER_WAS_ACTIVE=1 || true
unit_was_enabled twitch247.service && STREAMER_WAS_ENABLED=1 || true
unit_was_enabled twitch247-dashboard.service && DASHBOARD_WAS_ENABLED=1 || true
unit_was_enabled twitch247-watchdog.timer && WATCHDOG_TIMER_WAS_ENABLED=1 || true

if [[ -f "$APP_ROOT/database/twitch247.db" ]]; then
    STREAMER_WAS_PUBLISHING="$(
        runuser -u "$APP_USER" -- sqlite3 -readonly \
            "$APP_ROOT/database/twitch247.db" \
            "SELECT is_streaming FROM playback_state WHERE id=1;" \
            2>/dev/null || echo 0
    )"
    [[ "$STREAMER_WAS_PUBLISHING" == "1" ]] \
        || STREAMER_WAS_PUBLISHING=0
fi

MAINTENANCE_ACTIVE=1
info "Entering maintenance mode..."
stop_runtime_verified
backup_deployment
DEPLOYMENT_STARTED=1

# --- Deploy application files from the immutable snapshot ---
info "Deploying application files..."
replace_dir_contents "$STAGED_SOURCE/app" "$APP_ROOT/app"
replace_dir_contents "$STAGED_SOURCE/dashboard" "$APP_ROOT/dashboard"
replace_dir_contents "$STAGED_SOURCE/scripts" "$APP_ROOT/scripts"
replace_dir_contents "$STAGED_SOURCE/services" "$APP_ROOT/services"
install -o root -g root -m 0640 \
    "$STAGED_SOURCE/database/schema.sql" "$APP_ROOT/database/schema.sql"
install -o root -g root -m 0644 \
    "$STAGED_SOURCE/requirements.txt" "$APP_ROOT/requirements.txt"
install -o root -g "$APP_GROUP" -m 0640 \
    "$STAGED_SOURCE/config/config.env.example" \
    "$APP_ROOT/config/config.env.example"

if [[ ! -f "$APP_ROOT/config/config.env" ]]; then
    install -o root -g "$APP_GROUP" -m 0640 \
        "$STAGED_SOURCE/config/config.env.example" \
        "$APP_ROOT/config/config.env"
    warn "Config created at $APP_ROOT/config/config.env — edit TWITCH_STREAM_KEY before starting!"
else
    info "Config already exists, skipping."
fi

set_app_permissions

# --- Atomically select the prepared Python environment ---
info "Activating replacement Python virtual environment..."
switch_venv

# --- Initialize database ---
info "Initializing SQLite database..."
runuser -u "$APP_USER" -- env HOME=/tmp \
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
set_app_permissions

# --- Systemd services ---
info "Installing systemd services..."
install -o root -g root -m 0644 \
    "$STAGED_SOURCE/services/twitch247.service" \
    /etc/systemd/system/twitch247.service
install -o root -g root -m 0644 \
    "$STAGED_SOURCE/services/twitch247-dashboard.service" \
    /etc/systemd/system/twitch247-dashboard.service
install -o root -g root -m 0644 \
    "$STAGED_SOURCE/services/twitch247-watchdog.service" \
    /etc/systemd/system/twitch247-watchdog.service
install -o root -g root -m 0644 \
    "$STAGED_SOURCE/services/twitch247-watchdog.timer" \
    /etc/systemd/system/twitch247-watchdog.timer

systemctl daemon-reload
systemctl enable twitch247.service
systemctl enable twitch247-dashboard.service
systemctl enable twitch247-watchdog.timer

info "Restoring services that were active before the upgrade..."
if (( STREAMER_WAS_ACTIVE == 1 )); then
    systemctl start twitch247.service
    wait_for_unit_stability twitch247.service 10
    if (( STREAMER_WAS_PUBLISHING == 1 )); then
        wait_for_rtmp_publish
    fi
fi
if (( DASHBOARD_WAS_ACTIVE == 1 )); then
    systemctl start twitch247-dashboard.service
    wait_for_unit_stability twitch247-dashboard.service 5
    wait_for_dashboard_health
fi
if (( WATCHDOG_TIMER_WAS_ACTIVE == 1 )); then
    systemctl start twitch247-watchdog.timer
    systemctl is-active --quiet twitch247-watchdog.timer
fi

# The new release is healthy. From here on, cleanup failures must never trigger
# a rollback of running services.
MAINTENANCE_ACTIVE=0
DEPLOYMENT_STARTED=0
VENV_SWITCHED=0

if [[ -n "$VENV_LEGACY" && -d "$VENV_LEGACY" ]]; then
    safe_remove_tree "$VENV_LEGACY" 2>/dev/null \
        || warn "Could not remove the previous legacy virtual environment."
fi
if [[ -n "$VENV_OLD_RELEASE" \
    && "$VENV_OLD_RELEASE" == "$VENV_RELEASE_ROOT/"* \
    && "$VENV_OLD_RELEASE" != "$VENV_NEW" \
    && -d "$VENV_OLD_RELEASE" ]]; then
    safe_remove_tree "$VENV_OLD_RELEASE" 2>/dev/null \
        || warn "Could not remove the previous virtual environment release."
fi

safe_remove_tree "$WORK_DIR" 2>/dev/null \
    || warn "Could not remove the temporary installation backup."
WORK_DIR=""
trap - EXIT

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
warn "Make sure the configured dashboard port is accessible if you want remote access."
