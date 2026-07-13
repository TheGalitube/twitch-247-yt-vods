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
TWITCH_GQL_CLIENT_ID="${TWITCH_GQL_CLIENT_ID:-kimne78kx3ncx6brgo4mv6wki5h1ko}"
PLAYBACK_FRESH_SECONDS="${PLAYBACK_FRESH_SECONDS:-120}"
TWITCH_OFFLINE_RECOVERY_THRESHOLD="${TWITCH_OFFLINE_RECOVERY_THRESHOLD:-3}"
TWITCH_OFFLINE_STATE_FILE="${APP_ROOT}/logs/watchdog-twitch-offline-count"

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

has_healthy_rtmp_socket() {
    local pid
    for pid in $(pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true); do
        if ! ss -tanp 2>/dev/null | grep "pid=${pid}," | grep -q ":1935"; then
            continue
        fi

        if ss -tanp state close-wait 2>/dev/null | grep -q "pid=${pid},"; then
            continue
        fi

        return 0
    done

    return 1
}

restart_streamer_after_grace() {
    local reason="$1"
    log "WARN: ${reason} — waiting 20s for streamer self-heal"
    sleep 20

    if has_healthy_rtmp_socket; then
        log "INFO: RTMP connection recovered during grace period"
        exit 0
    fi

    log "WARN: ${reason} persists — restarting streamer"
    systemctl restart twitch247.service
    exit 0
}

twitch_live_state() {
    local channel="${TWITCH_CHANNEL:-}"
    if [[ -z "$channel" ]]; then
        echo "unknown"
        return 0
    fi

    local payload response
    payload=$(python3 - "$channel" <<'PY'
import json
import sys

print(json.dumps({
    "query": (
        "query($login:String!){"
        "user(login:$login){login stream{type createdAt}}"
        "}"
    ),
    "variables": {"login": sys.argv[1].lstrip("@")},
}))
PY
)

    response=$(curl -fsS \
        -H "Client-ID: ${TWITCH_GQL_CLIENT_ID}" \
        -H "Content-Type: application/json" \
        --data "$payload" \
        "https://gql.twitch.tv/gql" 2>/dev/null) || {
        echo "unknown"
        return 0
    }

    TWITCH_RESPONSE="$response" python3 - <<'PY'
import json
import os
import sys

try:
    data = json.loads(os.environ["TWITCH_RESPONSE"])
    stream = ((data.get("data") or {}).get("user") or {}).get("stream")
except Exception:
    print("unknown")
    raise SystemExit

if stream and stream.get("type") == "live":
    print("live")
else:
    print("offline")
PY
}

reset_twitch_offline_count() {
    rm -f "$TWITCH_OFFLINE_STATE_FILE" 2>/dev/null || true
}

increment_twitch_offline_count() {
    local count=0

    if [[ -f "$TWITCH_OFFLINE_STATE_FILE" ]]; then
        count="$(cat "$TWITCH_OFFLINE_STATE_FILE" 2>/dev/null || echo 0)"
        [[ "$count" =~ ^[0-9]+$ ]] || count=0
    fi

    count=$((count + 1))
    echo "$count" > "$TWITCH_OFFLINE_STATE_FILE" 2>/dev/null || true
    echo "$count"
}

playback_recently_saved() {
    [[ -f "$DB" ]] || return 1

    local last_save epoch now age
    last_save="$(sqlite3 "$DB" "SELECT COALESCE(last_save_at, '') FROM playback_state WHERE id=1;" 2>/dev/null || echo "")"
    [[ -n "$last_save" ]] || return 1

    epoch="$(date -d "${last_save} UTC" +%s 2>/dev/null || echo 0)"
    [[ "$epoch" =~ ^[0-9]+$ && "$epoch" -gt 0 ]] || return 1

    now="$(date +%s)"
    age=$((now - epoch))
    [[ "$age" -ge 0 && "$age" -le "$PLAYBACK_FRESH_SECONDS" ]]
}

recover_rtmp_publish_after_grace() {
    local reason="$1"
    log "WARN: ${reason} — waiting 20s before RTMP publish recovery"
    sleep 20

    local state
    state="$(twitch_live_state)"
    if [[ "$state" == "live" ]]; then
        reset_twitch_offline_count
        log "INFO: Twitch channel is live after grace period"
        exit 0
    fi
    if [[ "$state" == "unknown" ]]; then
        log "WARN: Could not verify Twitch live state, leaving RTMP process running"
        exit 0
    fi

    if has_healthy_rtmp_socket && playback_recently_saved; then
        local offline_count
        offline_count="$(increment_twitch_offline_count)"

        if (( offline_count < TWITCH_OFFLINE_RECOVERY_THRESHOLD )); then
            log "WARN: Twitch still reports channel offline, but local RTMP socket is healthy and playback state is fresh — skipping RTMP recycle (${offline_count}/${TWITCH_OFFLINE_RECOVERY_THRESHOLD})"
            exit 0
        fi

        log "WARN: Twitch offline state persisted with healthy local RTMP (${offline_count}/${TWITCH_OFFLINE_RECOVERY_THRESHOLD}) — recycling RTMP output"
    else
        reset_twitch_offline_count
    fi

    log "WARN: Twitch still reports channel offline — restarting RTMP output only"
    for pid in $(pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true); do
        kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 5
    for pid in $(pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true); do
        kill -KILL "$pid" 2>/dev/null || true
    done
    reset_twitch_offline_count
    exit 0
}

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
FFMPEG_COUNT=$( (pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true) | wc -l )
if [[ "$FFMPEG_COUNT" -eq 0 ]]; then
    IS_STREAMING=$(sqlite3 "$DB" \
        "SELECT is_streaming FROM playback_state WHERE id=1;" 2>/dev/null || echo 0)
    if [[ "$IS_STREAMING" == "1" ]]; then
        restart_streamer_after_grace "is_streaming=1 but no ffmpeg process"
    fi
fi

for pid in $(pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true); do
    RTMP_SOCKET=$(ss -tanp 2>/dev/null | grep "pid=${pid}," | grep ":1935" || true)
    if [[ -z "$RTMP_SOCKET" ]]; then
        restart_streamer_after_grace "RTMP ffmpeg pid ${pid} has no Twitch TCP connection"
    fi

    if ss -tanp state close-wait 2>/dev/null | grep -q "pid=${pid},"; then
        restart_streamer_after_grace "RTMP ffmpeg pid ${pid} has Twitch TCP connection in CLOSE-WAIT"
    fi
done

IS_STREAMING=$(sqlite3 "$DB" \
    "SELECT is_streaming FROM playback_state WHERE id=1;" 2>/dev/null || echo 0)
if [[ "$IS_STREAMING" == "1" ]]; then
    TWITCH_STATE="$(twitch_live_state)"
    if [[ "$TWITCH_STATE" == "offline" ]]; then
        recover_rtmp_publish_after_grace "local RTMP socket is healthy but Twitch reports ${TWITCH_CHANNEL:-channel} offline"
    elif [[ "$TWITCH_STATE" == "live" ]]; then
        reset_twitch_offline_count
    elif [[ "$TWITCH_STATE" == "unknown" ]]; then
        log "WARN: Twitch live-state check unavailable"
    fi
fi

log "INFO: Health check passed"
