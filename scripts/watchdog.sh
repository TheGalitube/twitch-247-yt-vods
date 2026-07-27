#!/usr/bin/env bash
# Twitch247 Watchdog Script
# - Verifies the streamer service is running
# - Proactively restarts before Twitch's 48-hour limit
# - Checks dashboard health

set -euo pipefail

APP_ROOT="/opt/twitch247"
CONFIG="${APP_ROOT}/config/config.env"
MAX_STREAM_HOURS="${MAX_STREAM_HOURS:-47}"
UPTIME_RESTART_COOLDOWN_SECONDS="${UPTIME_RESTART_COOLDOWN_SECONDS:-900}"
UPTIME_RESTART_STATE_FILE="/run/twitch247-watchdog-uptime-restart"
DASHBOARD_PORT="${DASHBOARD_PORT:-}"
TWITCH_GQL_CLIENT_ID="${TWITCH_GQL_CLIENT_ID:-kimne78kx3ncx6brgo4mv6wki5h1ko}"
RTMP_SELF_HEAL_GRACE_SECONDS="${RTMP_SELF_HEAL_GRACE_SECONDS:-45}"
TWITCH_OFFLINE_CONFIRM_SECONDS="${TWITCH_OFFLINE_CONFIRM_SECONDS:-20}"
RTMP_RECOVERY_TIMEOUT_SECONDS="${RTMP_RECOVERY_TIMEOUT_SECONDS:-40}"
RTMP_RECYCLE_COOLDOWN_SECONDS="${RTMP_RECYCLE_COOLDOWN_SECONDS:-300}"
RTMP_RECYCLE_STATE_FILE="/run/twitch247-watchdog-rtmp-recycle"
TWITCH_STATE="unknown"
TWITCH_STARTED_AT=""

read_config_value() {
    local key="$1"
    [[ -f "$CONFIG" ]] || return 0
    awk -v wanted="$key" '
        $0 ~ "^[[:space:]]*" wanted "[[:space:]]*=" {
            sub("^[[:space:]]*" wanted "[[:space:]]*=[[:space:]]*", "")
            sub("[[:space:]]+$", "")
            if (($0 ~ /^".*"$/) || ($0 ~ /^\047.*\047$/)) {
                $0 = substr($0, 2, length($0) - 2)
            }
            print
            exit
        }
    ' "$CONFIG"
}

# Parse only the non-secret values this script needs. Never source a config
# file from this root-owned watchdog.
TWITCH_CHANNEL="${TWITCH_CHANNEL:-$(read_config_value TWITCH_CHANNEL)}"
DASHBOARD_PORT="${DASHBOARD_PORT:-$(read_config_value DASHBOARD_PORT)}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
DB="${DB_PATH:-$(read_config_value DB_PATH)}"
DB="${DB:-${APP_ROOT}/database/twitch247.db}"
LOG_DIR="${LOG_DIR:-$(read_config_value LOG_DIR)}"
LOG_DIR="${LOG_DIR:-${APP_ROOT}/logs}"

# Prevent a manual run from racing the systemd timer.
exec 9>"/run/twitch247-watchdog.lock"
flock -n 9 || exit 0

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $*"
}

if [[ ! "$DASHBOARD_PORT" =~ ^[0-9]{1,5}$ ]] \
    || (( 10#$DASHBOARD_PORT < 1 || 10#$DASHBOARD_PORT > 65535 )); then
    log "WARN: Invalid dashboard port in configuration; using 8080"
    DASHBOARD_PORT=8080
fi

has_healthy_rtmp_socket() {
    local pid sockets
    sockets="$(ss -Htanp state established 2>/dev/null || true)"
    for pid in $(pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true); do
        if grep "pid=${pid}," <<< "$sockets" | grep -q ":1935"; then
            return 0
        fi
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
        --connect-timeout 3 \
        --max-time 8 \
        --retry 1 \
        --retry-delay 1 \
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
    if not isinstance(data, dict) or data.get("errors"):
        raise ValueError("GraphQL error response")
    payload = data.get("data")
    if not isinstance(payload, dict) or "user" not in payload:
        raise ValueError("GraphQL data.user missing")
    user = payload["user"]
    if not isinstance(user, dict) or "stream" not in user:
        raise ValueError("GraphQL user.stream missing")
    stream = user["stream"]
except Exception:
    print("unknown|")
    raise SystemExit

if isinstance(stream, dict) and stream.get("type") == "live":
    print(f"live|{stream.get('createdAt') or ''}")
elif stream is None:
    print("offline|")
else:
    print("unknown|")
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

    local start_epoch now_epoch elapsed_hours last_restart
    start_epoch=$(date -d "$TWITCH_STARTED_AT" +%s 2>/dev/null || echo 0)
    now_epoch=$(date +%s)
    if [[ ! "$start_epoch" =~ ^[0-9]+$ ]] || (( start_epoch <= 0 || now_epoch < start_epoch )); then
        log "WARN: Invalid Twitch broadcast createdAt '${TWITCH_STARTED_AT}' — skipping 47h restart this cycle"
        return 0
    fi

    elapsed_hours=$(( (now_epoch - start_epoch) / 3600 ))
    if (( elapsed_hours >= MAX_STREAM_HOURS )); then
        last_restart="$(cat "$UPTIME_RESTART_STATE_FILE" 2>/dev/null || echo 0)"
        [[ "$last_restart" =~ ^[0-9]+$ ]] || last_restart=0
        if (( now_epoch - last_restart < UPTIME_RESTART_COOLDOWN_SECONDS )); then
            log "WARN: Twitch still reports ${elapsed_hours}h, but 47h restart cooldown is active"
            return 0
        fi
        printf '%s\n' "$now_epoch" > "$UPTIME_RESTART_STATE_FILE"
        chmod 600 "$UPTIME_RESTART_STATE_FILE"
        log "INFO: Twitch broadcast running ${elapsed_hours}h — proactive restart for 48h limit"
        systemctl restart twitch247.service
        log "INFO: Service restarted for 48h Twitch limit"
        exit 0
    fi

    log "INFO: Twitch broadcast uptime ${elapsed_hours}h / ${MAX_STREAM_HOURS}h limit"
}

clear_legacy_offline_state() {
    rm -f "${LOG_DIR}/watchdog-twitch-offline-count" 2>/dev/null || true
}

recycle_rtmp_output() {
    local -a targets=()
    local pid start_time target expected_start current_start

    while read -r pid; do
        [[ -n "$pid" && -r "/proc/${pid}/stat" ]] || continue
        start_time="$(awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || true)"
        [[ "$start_time" =~ ^[0-9]+$ ]] || continue
        targets+=("${pid}:${start_time}")
    done < <(pgrep -u twitch247 -f "ffmpeg.*live.twitch.tv" 2>/dev/null || true)

    if (( ${#targets[@]} == 0 )); then
        log "WARN: No RTMP publisher found to recycle — restarting service"
        systemctl restart twitch247.service
        return 1
    fi

    for target in "${targets[@]}"; do
        pid="${target%%:*}"
        kill -TERM "$pid" 2>/dev/null || true
    done

    sleep 5

    # Only force-kill the exact original processes. A replacement publisher may
    # already have started and must never be caught by this second pass.
    for target in "${targets[@]}"; do
        pid="${target%%:*}"
        expected_start="${target##*:}"
        [[ -r "/proc/${pid}/stat" ]] || continue
        current_start="$(awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || true)"
        if [[ "$current_start" == "$expected_start" ]] \
            && tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null \
                | grep -q "ffmpeg.*live.twitch.tv"; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done

    local waited=0 recovered_socket=0
    while (( waited < RTMP_RECOVERY_TIMEOUT_SECONDS )); do
        sleep 5
        waited=$((waited + 5))
        if has_healthy_rtmp_socket; then
            recovered_socket=1
            break
        fi
    done

    if (( recovered_socket == 1 )); then
        # Give Twitch a short moment to expose the new publish session, then
        # perform one bounded API verification.
        sleep 5
        refresh_twitch_stream_info
        if [[ "$TWITCH_STATE" == "live" ]]; then
            log "INFO: RTMP publisher recovered and Twitch is live"
            return 0
        fi
        if [[ "$TWITCH_STATE" == "unknown" ]]; then
            log "INFO: Replacement RTMP socket is established; Twitch API status unavailable"
            return 0
        fi
        log "WARN: Replacement RTMP socket is established; Twitch live state will be rechecked next cycle"
        return 0
    fi

    log "WARN: RTMP-only recovery did not establish a new publisher — restarting service"
    systemctl restart twitch247.service
    return 1
}

recover_rtmp_publish_after_grace() {
    local reason="$1"
    local now_epoch last_recycle
    log "WARN: ${reason} — confirming for ${TWITCH_OFFLINE_CONFIRM_SECONDS}s"
    sleep "$TWITCH_OFFLINE_CONFIRM_SECONDS"

    refresh_twitch_stream_info
    if [[ "$TWITCH_STATE" == "live" ]]; then
        log "INFO: Twitch channel is live after grace period"
        exit 0
    fi
    if [[ "$TWITCH_STATE" == "unknown" ]]; then
        log "WARN: Could not verify Twitch live state, leaving RTMP process running"
        exit 0
    fi

    now_epoch="$(date +%s)"
    last_recycle="$(cat "$RTMP_RECYCLE_STATE_FILE" 2>/dev/null || echo 0)"
    [[ "$last_recycle" =~ ^[0-9]+$ ]] || last_recycle=0
    if (( now_epoch - last_recycle < RTMP_RECYCLE_COOLDOWN_SECONDS )); then
        log "WARN: Twitch is still offline, but the RTMP recycle cooldown is active"
        exit 0
    fi

    printf '%s\n' "$now_epoch" > "$RTMP_RECYCLE_STATE_FILE"
    chmod 600 "$RTMP_RECYCLE_STATE_FILE"
    log "WARN: Twitch is still offline after confirmation — recycling RTMP output"
    recycle_rtmp_output || true
    clear_legacy_offline_state
    exit 0
}

# Ensure main service is active
if ! systemctl is-active --quiet twitch247.service; then
    log "ERROR: twitch247.service is not running — restarting"
    systemctl restart twitch247.service
    exit 0
fi

# Check dashboard health
if ! curl -sf \
    --connect-timeout 2 \
    --max-time 5 \
    "http://127.0.0.1:${DASHBOARD_PORT}/health" > /dev/null 2>&1; then
    log "WARN: Dashboard not responding — restarting dashboard"
    systemctl restart twitch247-dashboard.service 2>/dev/null || true
fi

IS_STREAMING=$(runuser -u twitch247 -- sqlite3 -readonly "$DB" \
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
elif ! has_healthy_rtmp_socket; then
    if [[ "$IS_STREAMING" == "1" ]]; then
        restart_streamer_after_grace \
            "RTMP publisher has no established Twitch TCP connection"
    fi
fi

if [[ "$IS_STREAMING" == "1" ]]; then
    if [[ "$TWITCH_STATE" == "offline" ]]; then
        recover_rtmp_publish_after_grace "local RTMP socket is healthy but Twitch reports ${TWITCH_CHANNEL:-channel} offline"
    elif [[ "$TWITCH_STATE" == "live" ]]; then
        clear_legacy_offline_state
    elif [[ "$TWITCH_STATE" == "unknown" ]]; then
        clear_legacy_offline_state
        log "WARN: Twitch live-state check unavailable"
    fi
else
    clear_legacy_offline_state
fi

log "INFO: Health check passed"
