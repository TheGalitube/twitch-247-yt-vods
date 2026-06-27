#!/usr/bin/env bash
# Twitch247 Watchdog Script
# - Verifies the streamer service is running
# - Proactively restarts before Twitch's 48-hour limit
# - Checks dashboard health

set -euo pipefail

APP_ROOT="/opt/twitch247"
CONFIG="${APP_ROOT}/config/config.env"
LOG="${APP_ROOT}/logs/watchdog.log"
MAX_STREAM_HOURS=47
DASHBOARD_PORT=8080

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $*" | tee -a "$LOG"
}

# Load dashboard port from config if available
if [[ -f "$CONFIG" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG" 2>/dev/null || true
    DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
fi

mkdir -p "${APP_ROOT}/logs"

# Ensure main service is active
if ! systemctl is-active --quiet twitch247.service; then
    log "ERROR: twitch247.service is not running — restarting"
    systemctl restart twitch247.service
    exit 0
fi

# Check dashboard health
if ! curl -sf "http://127.0.0.1:${DASHBOARD_PORT}/health" > /dev/null 2>&1; then
    log "WARN: Dashboard not responding — restarting dashboard"
    systemctl restart twitch247-dashboard.service 2>/dev/null || true
fi

# Proactive 48-hour Twitch restart
# Read stream_started_at from SQLite
DB="${APP_ROOT}/database/twitch247.db"
if [[ -f "$DB" ]]; then
    STREAM_STARTED=$(sqlite3 "$DB" \
        "SELECT stream_started_at FROM playback_state WHERE id=1;" 2>/dev/null || echo "")

    if [[ -n "$STREAM_STARTED" && "$STREAM_STARTED" != "NULL" ]]; then
        START_EPOCH=$(date -d "$STREAM_STARTED UTC" +%s 2>/dev/null || echo 0)
        NOW_EPOCH=$(date +%s)
        ELAPSED_HOURS=$(( (NOW_EPOCH - START_EPOCH) / 3600 ))

        if [[ "$ELAPSED_HOURS" -ge "$MAX_STREAM_HOURS" ]]; then
            log "INFO: Stream running ${ELAPSED_HOURS}h — proactive restart for 48h limit"
            # Reset stream_started_at so next cycle starts fresh timer
            sqlite3 "$DB" \
                "UPDATE playback_state SET stream_started_at = NULL WHERE id=1;" 2>/dev/null || true
            systemctl restart twitch247.service
            log "INFO: Service restarted for 48h Twitch limit"
            exit 0
        fi

        log "INFO: Stream uptime ${ELAPSED_HOURS}h / ${MAX_STREAM_HOURS}h limit"
    fi
fi

# Verify ffmpeg is running (streamer should have an active ffmpeg child)
FFMPEG_COUNT=$(pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null | wc -l || echo 0)
if [[ "$FFMPEG_COUNT" -eq 0 ]]; then
    IS_STREAMING=$(sqlite3 "$DB" \
        "SELECT is_streaming FROM playback_state WHERE id=1;" 2>/dev/null || echo 0)
    if [[ "$IS_STREAMING" == "1" ]]; then
        log "WARN: is_streaming=1 but no ffmpeg process — restarting streamer"
        systemctl restart twitch247.service
        exit 0
    fi
fi

for pid in $(pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true); do
    RTMP_SOCKET=$(ss -tanp 2>/dev/null | grep "pid=${pid}," | grep ":1935" || true)
    if [[ -z "$RTMP_SOCKET" ]]; then
        log "WARN: RTMP ffmpeg pid ${pid} has no Twitch TCP connection — restarting streamer"
        systemctl restart twitch247.service
        exit 0
    fi

    if ss -tanp state close-wait 2>/dev/null | grep -q "pid=${pid},"; then
        log "WARN: RTMP ffmpeg pid ${pid} has Twitch TCP connection in CLOSE-WAIT — restarting streamer"
        systemctl restart twitch247.service
        exit 0
    fi
done

log "INFO: Health check passed"
