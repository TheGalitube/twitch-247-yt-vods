#!/usr/bin/env bash
# Twitch247 Watchdog Script
# - Verifies the streamer service is running
# - Proactively restarts before Twitch's 48-hour limit
# - Checks dashboard health

set -euo pipefail

APP_ROOT="/opt/twitch247"
CONFIG="${APP_ROOT}/config/config.env"
LOG="${APP_ROOT}/logs/watchdog.log"
DB="${APP_ROOT}/database/twitch247.db"
MAX_STREAM_HOURS="${MAX_STREAM_HOURS:-47}"
DASHBOARD_PORT=8080
TWITCH_GQL_CLIENT_ID="${TWITCH_GQL_CLIENT_ID:-kimne78kx3ncx6brgo4mv6wki5h1ko}"
PLAYBACK_FRESH_SECONDS="${PLAYBACK_FRESH_SECONDS:-120}"
RTMP_SELF_HEAL_GRACE_SECONDS="${RTMP_SELF_HEAL_GRACE_SECONDS:-45}"
TWITCH_OFFLINE_RECOVERY_THRESHOLD="${TWITCH_OFFLINE_RECOVERY_THRESHOLD:-3}"
TWITCH_OFFLINE_STATE_FILE="${APP_ROOT}/logs/watchdog-twitch-offline-count"
TWITCH_STATE="unknown"
TWITCH_STARTED_AT=""

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
    log "WARN: ${reason} — waiting ${RTMP_SELF_HEAL_GRACE_SECONDS}s for streamer self-heal"
    sleep "$RTMP_SELF_HEAL_GRACE_SECONDS"

    if has_healthy_rtmp_socket; then
        log "INFO: RTMP connection recovered during grace period"
        exit 0
    fi

    if playback_recently_saved; then
        log "WARN: ${reason} persists, but playback state is fresh — leaving service running for app self-heal"
        exit 0
    fi

    log "WARN: ${reason} persists — restarting streamer"
    systemctl restart twitch247.service
    exit 0
}

refresh_twitch_stream_info() {
    local channel="${TWITCH_CHANNEL:-}"
    if [[ -z "$channel" ]]; then
        TWITCH_STATE="unknown"
        TWITCH_STARTED_AT=""
        return 0
    fi

    local payload response parsed
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
        TWITCH_STATE="unknown"
        TWITCH_STARTED_AT=""
        return 0
    }

    parsed=$(TWITCH_RESPONSE="$response" python3 - <<'PY'
import json
import os

try:
    data = json.loads(os.environ["TWITCH_RESPONSE"])
    stream = ((data.get("data") or {}).get("user") or {}).get("stream")
except Exception:
    print("unknown|")
    raise SystemExit

if stream and stream.get("type") == "live":
    print(f"live|{stream.get('createdAt') or ''}")
else:
    print("offline|")
PY
    )

    IFS='|' read -r TWITCH_STATE TWITCH_STARTED_AT <<< "$parsed"
    TWITCH_STATE="${TWITCH_STATE:-unknown}"
}

check_twitch_uptime_limit() {
    if [[ "$TWITCH_STATE" == "unknown" ]]; then
        log "WARN: Twitch broadcast uptime unavailable — skipping 47h restart this cycle"
        return 0
    fi

    if [[ "$TWITCH_STATE" == "offline" ]]; then
        log "INFO: Twitch channel is offline — skipping 47h restart and deferring to RTMP recovery"
        return 0
    fi

    if [[ -z "$TWITCH_STARTED_AT" ]]; then
        log "WARN: Twitch reports live without createdAt — skipping 47h restart this cycle"
        return 0
    fi

    local start_epoch now_epoch elapsed_hours
    start_epoch=$(date -d "$TWITCH_STARTED_AT" +%s 2>/dev/null || echo 0)
    now_epoch=$(date +%s)
    if [[ ! "$start_epoch" =~ ^[0-9]+$ ]] || (( start_epoch <= 0 || now_epoch < start_epoch )); then
        log "WARN: Invalid Twitch broadcast createdAt '${TWITCH_STARTED_AT}' — skipping 47h restart this cycle"
        return 0
    fi

    elapsed_hours=$(( (now_epoch - start_epoch) / 3600 ))
    if (( elapsed_hours >= MAX_STREAM_HOURS )); then
        log "INFO: Twitch broadcast running ${elapsed_hours}h — proactive restart for 48h limit"
        systemctl restart twitch247.service
        log "INFO: Service restarted for 48h Twitch limit"
        exit 0
    fi

    log "INFO: Twitch broadcast uptime ${elapsed_hours}h / ${MAX_STREAM_HOURS}h limit"
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

    refresh_twitch_stream_info
    if [[ "$TWITCH_STATE" == "live" ]]; then
        reset_twitch_offline_count
        log "INFO: Twitch channel is live after grace period"
        exit 0
    fi
    if [[ "$TWITCH_STATE" == "unknown" ]]; then
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

IS_STREAMING=$(sqlite3 "$DB" \
    "SELECT is_streaming FROM playback_state WHERE id=1;" 2>/dev/null || echo 0)
if [[ "$IS_STREAMING" == "1" ]]; then
    refresh_twitch_stream_info
    check_twitch_uptime_limit
fi

# Verify ffmpeg is running (streamer should have an active ffmpeg child)
FFMPEG_COUNT=$( (pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true) | wc -l )
if [[ "$FFMPEG_COUNT" -eq 0 ]]; then
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

if [[ "$IS_STREAMING" == "1" ]]; then
    if [[ "$TWITCH_STATE" == "offline" ]]; then
        recover_rtmp_publish_after_grace "local RTMP socket is healthy but Twitch reports ${TWITCH_CHANNEL:-channel} offline"
    elif [[ "$TWITCH_STATE" == "live" ]]; then
        reset_twitch_offline_count
    elif [[ "$TWITCH_STATE" == "unknown" ]]; then
        log "WARN: Twitch live-state check unavailable"
    fi
fi

log "INFO: Health check passed"
