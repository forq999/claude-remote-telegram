#!/bin/bash
set -euo pipefail

# --- 크론 환경에서 PATH 설정 ---
export PATH="$HOME/.local/bin:$PATH"

# === 설정 (서버별 수정) ===
SERVER_NAME="server-a"
BOT_API_URL="https://123.45.67.89:8443"
API_TOKEN="your-shared-token"
DEFAULT_TIMEOUT=1800
ALLOWED_PATHS="/home/user/projects,/opt/work"
ALIASES="front=/home/user/projects/frontend,api=/home/user/projects/backend/api"
PID_DIR="/tmp/claude-sessions"
LOG_FILE="/tmp/claude-agent.log"
# === 설정 끝 ===

# --- 동시 실행 방지 ---
exec 9>/tmp/claude-agent.lock
flock -n 9 || exit 0

mkdir -p "$PID_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

# --- 경로 검증 ---
validate_path() {
    local path="$1"
    if [[ "$path" =~ [\;\`\$\(\)\|\&\>\<] ]]; then
        log "REJECTED: shell metachar in path: $path"
        return 1
    fi
    if [[ "$path" == *".."* ]]; then
        log "REJECTED: path traversal in path: $path"
        return 1
    fi
    IFS=',' read -ra PATHS <<< "$ALLOWED_PATHS"
    for allowed in "${PATHS[@]}"; do
        allowed="${allowed%/}"
        if [[ "$path" == "$allowed" || "$path" == "$allowed/"* ]]; then
            return 0
        fi
    done
    log "REJECTED: path not in ALLOWED_PATHS: $path"
    return 1
}

# --- API 호출 ---
api_call() {
    local method="$1" endpoint="$2"
    shift 2
    curl -sf -X "$method" \
        -H "Authorization: Bearer $API_TOKEN" \
        -H "Content-Type: application/json" \
        "$BOT_API_URL$endpoint" "$@"
}

# --- 프로젝트명 추출 ---
project_name() {
    basename "$1"
}

# --- 세션 시작 ---
start_session() {
    local path="$1" cmd_id="$2"

    if ! validate_path "$path"; then
        api_call POST "/api/commands/$cmd_id/done" \
            -d '{"status":"failed","error":"path not allowed"}'
        return
    fi

    local name
    name=$(project_name "$path")
    local pid_file="$PID_DIR/${name}.pid"

    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        log "Session already running: $name (PID $(cat "$pid_file"))"
        api_call POST "/api/commands/$cmd_id/done" \
            -d '{"status":"failed","error":"session already running locally"}'
        return
    fi

    # 서브셸에서 cd + 실행 (부모 셸 CWD 보호)
    local pid
    pid=$(cd "$path" && nohup claude --remote-control --dangerously-skip-permissions --name "$name" > "$PID_DIR/${name}.log" 2>&1 & echo $!)
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        api_call POST "/api/commands/$cmd_id/done" \
            -d '{"status":"failed","error":"cannot start session at path"}'
        return
    fi
    echo "$pid" > "$pid_file"
    echo "$path" > "$PID_DIR/${name}.path"
    touch "$PID_DIR/${name}.active"

    # 초기 CPU 시간 저장
    local cpu_time
    cpu_time=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null || echo "0")
    echo "$cpu_time" > "$PID_DIR/${name}.cpu"

    log "Started session: $name (PID $pid) at $path"
    api_call POST "/api/commands/$cmd_id/done" -d '{"status":"done"}'
}

# --- 세션 종료 ---
stop_session() {
    local path="$1" cmd_id="$2"
    local name
    name=$(project_name "$path")
    local pid_file="$PID_DIR/${name}.pid"

    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            log "Stopped session: $name (PID $pid)"
        fi
        rm -f "$pid_file" "$PID_DIR/${name}.cpu" \
              "$PID_DIR/${name}.active" "$PID_DIR/${name}.path"
    fi
    api_call POST "/api/commands/$cmd_id/done" -d '{"status":"done"}'
}

# --- 전체 세션 종료 ---
stop_all_sessions() {
    local cmd_id="$1"
    for pid_file in "$PID_DIR"/*.pid; do
        [ -f "$pid_file" ] || continue
        local pid
        pid=$(cat "$pid_file")
        kill "$pid" 2>/dev/null || true
        local base
        base=$(basename "$pid_file" .pid)
        rm -f "$pid_file" "$PID_DIR/${base}.cpu" \
              "$PID_DIR/${base}.active" "$PID_DIR/${base}.path"
        log "Stopped session: $base"
    done
    api_call POST "/api/commands/$cmd_id/done" -d '{"status":"done"}'
}

# --- 명령 폴링 및 처리 ---
process_commands() {
    local resp
    resp=$(api_call POST "/api/commands/$SERVER_NAME/claim") || return

    local count
    count=$(echo "$resp" | jq '.commands | length')
    [ "$count" -eq 0 ] && return

    echo "$resp" | jq -c '.commands[]' | while read -r cmd; do
        local id action path params
        id=$(echo "$cmd" | jq -r '.id')
        action=$(echo "$cmd" | jq -r '.action')
        path=$(echo "$cmd" | jq -r '.project_path // empty')
        params=$(echo "$cmd" | jq -r '.params // {}')

        case "$action" in
            start)
                [ -n "$path" ] && start_session "$path" "$id"
                ;;
            stop)
                if [ -n "$path" ]; then
                    stop_session "$path" "$id"
                else
                    stop_all_sessions "$id"
                fi
                ;;
            timeout)
                [ -n "$path" ] || continue
                local new_timeout
                new_timeout=$(echo "$params" | jq -r '.timeout_seconds // 1800')
                local name
                name=$(project_name "$path")
                echo "$new_timeout" > "$PID_DIR/${name}.timeout"
                api_call POST "/api/commands/$id/done" -d '{"status":"done"}'
                log "Timeout updated: $name -> ${new_timeout}s"
                ;;
        esac
    done
}

# --- Idle 체크 ---
check_idle_sessions() {
    for pid_file in "$PID_DIR"/*.pid; do
        [ -f "$pid_file" ] || continue
        local pid name timeout_val
        pid=$(cat "$pid_file")
        name=$(basename "$pid_file" .pid)

        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pid_file" "$PID_DIR/${name}.cpu" \
                  "$PID_DIR/${name}.active" "$PID_DIR/${name}.path"
            log "Session died: $name (PID $pid)"
            continue
        fi

        if [ -f "$PID_DIR/${name}.timeout" ]; then
            timeout_val=$(cat "$PID_DIR/${name}.timeout")
        else
            timeout_val=$DEFAULT_TIMEOUT
        fi

        local cpu_now cpu_prev
        cpu_now=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null || echo "0")
        cpu_prev=$(cat "$PID_DIR/${name}.cpu" 2>/dev/null || echo "0")

        if [ "$cpu_now" != "$cpu_prev" ]; then
            echo "$cpu_now" > "$PID_DIR/${name}.cpu"
            touch "$PID_DIR/${name}.active"
        fi

        local active_time now_time idle_secs
        active_time=$(stat -c %Y "$PID_DIR/${name}.active" 2>/dev/null || echo "0")
        now_time=$(date +%s)
        idle_secs=$((now_time - active_time))

        if [ "$idle_secs" -ge "$timeout_val" ]; then
            kill "$pid" 2>/dev/null || true
            rm -f "$pid_file" "$PID_DIR/${name}.cpu" \
                  "$PID_DIR/${name}.active" "$PID_DIR/${name}.path" \
                  "$PID_DIR/${name}.timeout"
            log "Idle timeout ($idle_secs >= $timeout_val): $name (PID $pid)"
        fi
    done
}

# --- 상태 보고 ---
report_status() {
    local sessions="[]"

    for pid_file in "$PID_DIR"/*.pid; do
        [ -f "$pid_file" ] || continue
        local pid name path active_time now_time idle_secs
        pid=$(cat "$pid_file")
        name=$(basename "$pid_file" .pid)
        path=$(cat "$PID_DIR/${name}.path" 2>/dev/null || echo "unknown")
        active_time=$(stat -c %Y "$PID_DIR/${name}.active" 2>/dev/null || echo "0")
        now_time=$(date +%s)
        idle_secs=$((now_time - active_time))

        sessions=$(echo "$sessions" | jq -c \
            --arg pp "$path" --arg pn "$name" --argjson pid "$pid" \
            --argjson idle "$idle_secs" \
            '. + [{"project_path":$pp,"project_name":$pn,"pid":$pid,"status":"running","idle_seconds":$idle}]')
    done

    api_call POST "/api/status" \
        -d "$(jq -nc --arg s "$SERVER_NAME" --argjson sess "$sessions" \
               '{server:$s,sessions:$sess}')" || true
}

# --- Heartbeat ---
send_heartbeat() {
    local paths_json aliases_json

    IFS=',' read -ra PATHS <<< "$ALLOWED_PATHS"
    paths_json=$(printf '%s\n' "${PATHS[@]}" | jq -R . | jq -sc .)

    aliases_json="{}"
    if [ -n "${ALIASES:-}" ]; then
        IFS=',' read -ra ALIAS_PAIRS <<< "$ALIASES"
        for pair in "${ALIAS_PAIRS[@]}"; do
            local key="${pair%%=*}" val="${pair#*=}"
            aliases_json=$(echo "$aliases_json" | jq -c --arg k "$key" --arg v "$val" '. + {($k):$v}')
        done
    fi

    api_call POST "/api/heartbeat" \
        -d "$(jq -nc --arg s "$SERVER_NAME" --argjson p "$paths_json" \
               --argjson a "$aliases_json" \
               '{server:$s,allowed_paths:$p,aliases:$a}')" || true
}

# === 메인 실행 ===
log "Agent run started"
process_commands
check_idle_sessions
report_status
send_heartbeat
log "Agent run completed"
