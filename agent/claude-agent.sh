#!/bin/bash
set -euo pipefail

# --- 크론 환경에서 PATH 설정 ---
export PATH="$HOME/.local/bin:$PATH"
CLAUDE_OPTS="--dangerously-skip-permissions --effort max --remote-control"

# === 설정 파일 로드 ===
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/agent.env" ]; then
    source "$SCRIPT_DIR/agent.env"
elif [ -f "$HOME/agent.env" ]; then
    source "$HOME/agent.env"
else
    echo "agent.env not found" >&2
    exit 1
fi

# ALLOWED_PATHS → ALLOWED_PATH 하위호환
ALLOWED_PATH="${ALLOWED_PATH:-${ALLOWED_PATHS:-}}"
LOG_FILE="${LOG_FILE:-$SCRIPT_DIR/claude-agent.log}"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

# --- 업데이트 ---
do_update() {
    local quiet="${1:-}"
    if [ -z "${AUTO_UPDATE_URL:-}" ]; then
        [ -z "$quiet" ] && echo "AUTO_UPDATE_URL not set in agent.env"
        return 1
    fi
    SELF="$(readlink -f "$0")"
    [ -z "$quiet" ] && echo "Fetching update from $AUTO_UPDATE_URL ..."
    local new_script
    new_script=$(curl -sf "$AUTO_UPDATE_URL" 2>/dev/null || true)
    if [ -z "$new_script" ]; then
        [ -z "$quiet" ] && echo "Failed to fetch update"
        return 1
    fi
    local old_hash new_hash
    old_hash=$(md5sum "$SELF" | awk '{print $1}')
    new_hash=$(echo "$new_script" | md5sum | awk '{print $1}')
    if [ "$old_hash" = "$new_hash" ]; then
        [ -z "$quiet" ] && echo "Already up to date"
        return 0
    fi
    echo "$new_script" > "$SELF"
    chmod +x "$SELF"
    [ -z "$quiet" ] && echo "Updated successfully"
    if [ -z "$quiet" ]; then
        log "Manual update applied"
    else
        log "Auto-updated script"
    fi
    return 0
}

# --- CLI 플래그 처리 (락 불필요) ---
case "${1:-}" in
    --update) do_update; exit $? ;;
esac

# --- 동시 실행 방지 ---
LOCK_FILE="${LOCK_FILE:-$SCRIPT_DIR/claude-agent.lock}"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

PID_DIR="${PID_DIR:-$SCRIPT_DIR/claude-agent-pids}"
mkdir -p "$PID_DIR"

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
    local allowed="${ALLOWED_PATH%/}"
    if [[ "$path" == "$allowed" || "$path" == "$allowed/"* ]]; then
        return 0
    fi
    log "REJECTED: path not in ALLOWED_PATH: $path"
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

    # resume ID가 있으면 --resume, 없으면 --name + 랜덤 ID로 새 세션
    local session_opt="" session_name=""
    local resume_id
    resume_id=$(echo "$3" | jq -r '.resume // empty' 2>/dev/null || true)
    if [ -n "$resume_id" ]; then
        session_name="$resume_id"
        session_opt="--resume $resume_id"
        log "Resuming session: $resume_id"
    else
        local ts
        ts=$(date '+%y%m%d_%H-%M-%S')
        session_name="${SERVER_NAME}_${name}_${ts}"
        session_opt="--name $session_name"
        log "New session: $session_name"
    fi

    # 경로가 없으면 생성 (validate_path 통과 후이므로 allowed 범위 내)
    [ -d "$path" ] || mkdir -p "$path"

    # script으로 TTY 제공
    (cd "$path" && script -qefc "claude $CLAUDE_OPTS $session_opt" "$PID_DIR/${name}.log" < /dev/null > /dev/null 2>&1) 9>&- &
    sleep 1
    # script이 실행한 실제 claude 프로세스 PID 찾기 (script 래퍼 자체 제외)
    local pid
    pid=$(pgrep -x claude -a 2>/dev/null | grep -F -- "$session_name" | awk '{print $1}' | head -1)
    if [ -z "$pid" ]; then
        api_call POST "/api/commands/$cmd_id/done" \
            -d '{"status":"failed","error":"cannot start session at path"}'
        return
    fi
    echo "$pid" > "$pid_file"
    echo "$path" > "$PID_DIR/${name}.path"
    echo "$session_name" > "$PID_DIR/${name}.sid"
    touch "$PID_DIR/${name}.active"

    # 세션 URL + UUID 추출 (최대 5초 대기)
    local session_url="" session_id="" wait_count=0
    # claude 프로젝트 디렉토리 (경로의 / 및 _ 를 - 로 변환)
    local claude_proj_dir="$HOME/.claude/projects/$(echo "$path" | sed 's|[/_]|-|g')"
    while [ $wait_count -lt 5 ]; do
        # URL from log
        if [ -z "$session_url" ]; then
            session_url=$(grep -oP 'https://claude\.ai/code/session_[A-Za-z0-9]+' "$PID_DIR/${name}.log" 2>/dev/null || true)
            session_url=$(echo "$session_url" | head -1)
        fi
        # UUID from .jsonl file (가장 최근 파일)
        if [ -z "$session_id" ] && [ -d "$claude_proj_dir" ]; then
            session_id=$(ls -t "$claude_proj_dir"/*.jsonl 2>/dev/null | head -1 | xargs -r basename | sed 's/\.jsonl//' || true)
        fi
        [ -n "$session_url" ] && [ -n "$session_id" ] && break
        sleep 1
        wait_count=$((wait_count + 1))
    done
    [ -n "$session_url" ] && echo "$session_url" > "$PID_DIR/${name}.url"
    [ -n "$session_id" ] && echo "$session_id" > "$PID_DIR/${name}.sid"
    log "Session URL: ${session_url:-none} (ID: ${session_name})"

    # done 전에 세션 정보를 먼저 DB에 보고
    api_call POST "/api/status" \
        -d "$(jq -nc --arg s "$SERVER_NAME" --arg pp "$path" --arg pn "$name" \
            --argjson pid "$pid" --arg url "${session_url:-}" --arg sid "$session_name" \
            '{server:$s,sessions:[{project_path:$pp,project_name:$pn,pid:$pid,status:"running",idle_seconds:0,session_url:$url,session_id:$sid}]}')" || true

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
              "$PID_DIR/${name}.active" "$PID_DIR/${name}.path" \
              "$PID_DIR/${name}.sid" "$PID_DIR/${name}.url"
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
              "$PID_DIR/${base}.active" "$PID_DIR/${base}.path" \
              "$PID_DIR/${base}.sid" "$PID_DIR/${base}.url"
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
                [ -n "$path" ] && start_session "$path" "$id" "$params"
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
            clean)
                # PID 파일로 추적 중인 세션만 종료 (로컬에서 직접 실행한 건 건드리지 않음)
                for pf in "$PID_DIR"/*.pid; do
                    [ -f "$pf" ] || continue
                    local cpid
                    cpid=$(cat "$pf")
                    kill "$cpid" 2>/dev/null || true
                    local cbase
                    cbase=$(basename "$pf" .pid)
                    rm -f "$pf" "$PID_DIR/${cbase}.cpu" \
                          "$PID_DIR/${cbase}.active" "$PID_DIR/${cbase}.path" \
                          "$PID_DIR/${cbase}.timeout" "$PID_DIR/${cbase}.sid" \
                          "$PID_DIR/${cbase}.url"
                    log "Clean: killed $cbase (PID $cpid)"
                done
                rm -f "$LOCK_FILE"
                api_call POST "/api/commands/$id/done" -d '{"status":"done"}'
                log "Clean completed"
                ;;
        esac
    done
}

# --- jsonl 파일 경로 조회 (없으면 빈 문자열) ---
find_session_jsonl() {
    local session_path=$1
    [ -z "$session_path" ] && return
    local encoded_path
    encoded_path=$(echo "$session_path" | sed 's|[/_]|-|g')
    ls -t "$HOME/.claude/projects/$encoded_path"/*.jsonl 2>/dev/null | head -1
}

# --- 활동 시간 계산 (jsonl mtime 우선, .active 파일 폴백) ---
# 사용법: get_activity_time <name> <session_path>
# Claude Code가 모든 이벤트(user/assistant/tool_use/tool_result)를
# jsonl 파일에 기록하므로 이 파일의 mtime이 가장 신뢰성 있는 활동 지표.
get_activity_time() {
    local name=$1 session_path=$2
    local jsonl_file
    jsonl_file=$(find_session_jsonl "$session_path")
    if [ -n "$jsonl_file" ] && [ -f "$jsonl_file" ]; then
        stat -c %Y "$jsonl_file"
        return
    fi
    stat -c %Y "$PID_DIR/${name}.active" 2>/dev/null || echo "0"
}

# --- CPU 기반 폴백: jsonl 을 찾지 못했을 때 .active 를 CPU 변화로 갱신 ---
# 안전망: 경로 인코딩 불일치, 파일 권한 문제, 잔존 세션 등 jsonl 탐색
# 실패 시에도 idle 감지가 동작하도록 기존 CPU 방식을 폴백으로 유지.
fallback_cpu_touch_active() {
    local name=$1 pid=$2
    local work_pid="$pid"
    # script 래퍼가 추적 중이면 실제 claude 자식의 CPU 를 사용
    if [ "$(cat /proc/$pid/comm 2>/dev/null)" = "script" ]; then
        local claude_child
        claude_child=$(pgrep -P "$pid" -x claude 2>/dev/null | head -1)
        [ -n "$claude_child" ] && work_pid="$claude_child"
    fi
    local cpu_now cpu_prev
    cpu_now=$(awk '{print $14+$15}' "/proc/$work_pid/stat" 2>/dev/null || echo "0")
    cpu_prev=$(cat "$PID_DIR/${name}.cpu" 2>/dev/null || echo "0")
    if [ "$cpu_now" != "$cpu_prev" ]; then
        echo "$cpu_now" > "$PID_DIR/${name}.cpu"
        touch "$PID_DIR/${name}.active"
    fi
}

# --- Idle 체크 ---
check_idle_sessions() {
    for pid_file in "$PID_DIR"/*.pid; do
        [ -f "$pid_file" ] || continue
        local pid name timeout_val session_path
        pid=$(cat "$pid_file")
        name=$(basename "$pid_file" .pid)
        session_path=$(cat "$PID_DIR/${name}.path" 2>/dev/null || echo "")

        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pid_file" "$PID_DIR/${name}.cpu" \
                  "$PID_DIR/${name}.active" "$PID_DIR/${name}.path" \
                  "$PID_DIR/${name}.timeout" "$PID_DIR/${name}.sid" \
                  "$PID_DIR/${name}.url"
            log "Session died: $name (PID $pid)"
            continue
        fi

        if [ -f "$PID_DIR/${name}.timeout" ]; then
            timeout_val=$(cat "$PID_DIR/${name}.timeout")
        else
            timeout_val=$DEFAULT_TIMEOUT
        fi

        # jsonl 을 찾지 못한 경우에만 CPU 기반 폴백으로 .active 갱신
        local jsonl_file
        jsonl_file=$(find_session_jsonl "$session_path")
        if [ -z "$jsonl_file" ]; then
            fallback_cpu_touch_active "$name" "$pid"
        fi

        local activity_time now_time idle_secs
        activity_time=$(get_activity_time "$name" "$session_path")
        now_time=$(date +%s)
        idle_secs=$((now_time - activity_time))

        if [ "$idle_secs" -ge "$timeout_val" ]; then
            # 구버전 호환: 추적 PID가 script 래퍼면 실제 claude 자식을 종료
            local kill_target="$pid"
            if [ "$(cat /proc/$pid/comm 2>/dev/null)" = "script" ]; then
                local claude_child
                claude_child=$(pgrep -P "$pid" -x claude 2>/dev/null | head -1)
                [ -n "$claude_child" ] && kill_target="$claude_child"
            fi
            kill "$kill_target" 2>/dev/null || true
            rm -f "$pid_file" "$PID_DIR/${name}.cpu" \
                  "$PID_DIR/${name}.active" "$PID_DIR/${name}.path" \
                  "$PID_DIR/${name}.timeout" "$PID_DIR/${name}.sid" \
                  "$PID_DIR/${name}.url"
            log "Idle timeout ($idle_secs >= $timeout_val): $name (PID $pid)"
        fi
    done
}

# --- 상태 보고 ---
report_status() {
    local sessions="[]"

    for pid_file in "$PID_DIR"/*.pid; do
        [ -f "$pid_file" ] || continue
        local pid name path activity_time now_time idle_secs
        pid=$(cat "$pid_file")
        name=$(basename "$pid_file" .pid)
        path=$(cat "$PID_DIR/${name}.path" 2>/dev/null || echo "unknown")
        activity_time=$(get_activity_time "$name" "$path")
        now_time=$(date +%s)
        idle_secs=$((now_time - activity_time))

        local url sid
        url=$(cat "$PID_DIR/${name}.url" 2>/dev/null || echo "")
        sid=$(cat "$PID_DIR/${name}.sid" 2>/dev/null || echo "")

        sessions=$(echo "$sessions" | jq -c \
            --arg pp "$path" --arg pn "$name" --argjson pid "$pid" \
            --argjson idle "$idle_secs" --arg url "$url" --arg sid "$sid" \
            '. + [{"project_path":$pp,"project_name":$pn,"pid":$pid,"status":"running","idle_seconds":$idle,"session_url":$url,"session_id":$sid}]')
    done

    api_call POST "/api/status" \
        -d "$(jq -nc --arg s "$SERVER_NAME" --argjson sess "$sessions" \
               '{server:$s,sessions:$sess}')" > /dev/null || true
}

# --- Heartbeat ---
send_heartbeat() {
    local aliases_json="{}"
    if [ -n "${ALIASES:-}" ]; then
        IFS=',' read -ra ALIAS_PAIRS <<< "$ALIASES"
        for pair in "${ALIAS_PAIRS[@]}"; do
            local key="${pair%%=*}" val="${pair#*=}"
            aliases_json=$(echo "$aliases_json" | jq -c --arg k "$key" --arg v "$val" '. + {($k):$v}')
        done
    fi

    api_call POST "/api/heartbeat" \
        -d "$(jq -nc --arg s "$SERVER_NAME" --arg p "${ALLOWED_PATH:-}" \
               --argjson a "$aliases_json" \
               '{server:$s,allowed_path:$p,aliases:$a}')" > /dev/null || true
}

# === 메인 실행 ===
# log "Agent run started"
process_commands
check_idle_sessions
report_status
send_heartbeat

# --- 자동 업데이트 (종료 직전) ---
if [ -n "${AUTO_UPDATE_URL:-}" ]; then
    UPDATE_CHECK="/tmp/claude-agent-update-check"
    UPDATE_INTERVAL="${UPDATE_INTERVAL:-86400}"
    last_check=$(stat -c %Y "$UPDATE_CHECK" 2>/dev/null || echo "0")
    now=$(date +%s)
    if [ $((now - last_check)) -ge "$UPDATE_INTERVAL" ]; then
        touch "$UPDATE_CHECK"
        do_update quiet || true
    fi
fi

# log "Agent run completed"
