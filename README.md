# Claude Remote Telegram

Telegram bot for managing Claude Code Remote sessions across multiple distributed servers.

## Architecture

```
[Telegram Bot] <-> User (mobile/PC)
      |
      |  (Telegram API)
      |
[Docker Container - Bot + API Server]
      |
      +-- Server A (cron agent) -> session: project-alpha
      |                          -> session: project-beta
      +-- Server B (cron agent) -> session: api-server
      +-- Server C (cron agent) -> session: ml-pipeline
```

- **Bot Server**: FastAPI + python-telegram-bot, SQLite, runs in Docker
- **Agent**: Single bash script + env file per server, runs via cron every 10s

## Setup

### 1. Create Telegram Bot

#### 1-1. BotFather에서 봇 생성

1. Telegram에서 `@BotFather`를 검색하여 대화 시작
2. `/newbot` 명령어 입력
3. 봇 표시 이름 입력 (예: `Claude Remote Manager`)
4. 봇 username 입력 (예: `my_claude_remote_bot`)
   - 반드시 `bot`으로 끝나야 함 (`_bot` 또는 `Bot`)
5. 생성 완료 시 **bot token**이 표시됨 (예: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)
   - 이 토큰을 `.env`의 `TELEGRAM_BOT_TOKEN`에 입력

#### 1-2. 봇 명령어 자동완성 등록

1. `@BotFather`에서 `/setcommands` 입력
2. 방금 만든 봇 선택
3. 아래 내용을 그대로 복사하여 전송:

```
run - Start session (server path|alias)
stop - Stop session (server [path|alias])
status - Show active sessions
servers - List registered servers
timeout - Set idle timeout (min server path|alias)
clean - Kill all sessions and cleanup (server)
help - Show help message
```

이렇게 하면 채팅창에서 `/` 입력 시 명령어 목록이 자동완성으로 표시됩니다.

#### 1-3. 봇 설명 설정 (선택)

`@BotFather`에서 추가 설정 가능:

- `/setdescription` - 봇 프로필에 표시되는 설명
- `/setabouttext` - 봇 정보 페이지의 상세 설명
- `/setuserpic` - 봇 프로필 사진

#### 1-4. 관리자 Telegram ID 확인

1. Telegram에서 `@userinfobot`을 검색하여 대화 시작
2. 아무 메시지나 전송
3. 응답으로 받은 숫자 ID를 `.env`의 `TELEGRAM_ADMIN_ID`에 입력
   - 이 ID를 가진 사용자만 봇 명령어를 실행할 수 있음

### 2. Bot Server (Docker)

```bash
cp .env.example .env
# Edit .env:
#   TELEGRAM_BOT_TOKEN=123456:ABC...
#   TELEGRAM_ADMIN_ID=987654321
#   API_TOKEN=your-shared-secret

docker compose up --build -d
```

### 3. Agent (Each Server)

```bash
# Copy script and config
cp agent/claude-agent.sh /home/user/claude-agent.sh
cp agent/agent.env.example /home/user/agent.env
chmod +x /home/user/claude-agent.sh
```

Edit `agent.env`:

```bash
SERVER_NAME="my-server"
BOT_API_URL="http://your-bot-server:8443"
API_TOKEN="your-shared-secret"
DEFAULT_TIMEOUT=1800
ALLOWED_PATHS="/home/user/projects"
ALIASES="front=/home/user/projects/frontend,api=/home/user/projects/backend"
PID_DIR="/tmp/claude-sessions"
LOG_FILE="/tmp/claude-agent.log"

# Auto-update (optional)
AUTO_UPDATE_URL="https://raw.githubusercontent.com/forq999/claude-remote-telegram/main/agent/claude-agent.sh"
UPDATE_INTERVAL=86400
```

Register in cron (10s interval):

```bash
# /etc/crontab
* * * * * username /home/user/claude-agent.sh
* * * * * username sleep 10 && /home/user/claude-agent.sh
* * * * * username sleep 20 && /home/user/claude-agent.sh
* * * * * username sleep 30 && /home/user/claude-agent.sh
* * * * * username sleep 40 && /home/user/claude-agent.sh
* * * * * username sleep 50 && /home/user/claude-agent.sh
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/run <server> <path\|alias>` | Start a new remote session |
| `/stop <server> [path\|alias]` | Stop session (omit path for all) |
| `/status [server]` | Show active sessions with uptime, idle status, session link |
| `/servers` | List registered servers, aliases, and run buttons |
| `/timeout <min> <server> <path\|alias>` | Change idle timeout |
| `/clean <server>` | Kill all sessions and cleanup |
| `/help` | Show help |

## Features

- **Multi-server, multi-project** session management
- **Path aliases** for quick access (`/run my-server front` instead of full path)
- **Auto-shutdown** on idle (configurable timeout, default 30min)
- **Session resume** — restart button resumes previous conversation via `--resume`
- **Session ID** — each session gets a unique name (`server_project_id`) for resume
- **Inline buttons** — Stop/Resume/New/Open on every notification
- **Session URL** — clickable link to `claude.ai/code` in status and start notifications
- **Real-time notifications** — on actual start/stop/fail (not on queue)
- **Markdown formatting** — clean, readable Telegram messages
- **Auto-discovery** — servers register on first heartbeat, removed after 2min offline
- **Auto-update** — agent script self-updates from GitHub (daily, configurable)
- **Concurrent safety** — `flock` prevents overlapping cron execution
- **Path validation** — prefix matching, traversal prevention, shell metachar blocking
- **Pseudo-TTY** — `script` command provides TTY for Claude Code in cron environment

## Session Lifecycle

```
/run server ssh          -> new session (--name server_ssh_a3f8k)
                         -> Started notification + [Stop] [Open]

/status                  -> uptime, idle status, session link, [Stop]

Stop button              -> Stopped notification + [Resume] [New]
  Resume button          -> resumes conversation (--resume server_ssh_a3f8k)
  New button             -> fresh session (--name server_ssh_b2c9d)

/servers                 -> server list + [Run] buttons (always new session)

idle 30min               -> auto-stop + Stopped notification
agent offline 2min       -> server removed from DB
```

## API Endpoints

All endpoints require `Authorization: Bearer {API_TOKEN}` header.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/commands/{server}/claim` | Atomically claim pending commands |
| POST | `/api/commands/{id}/done` | Report command completion (triggers notification) |
| POST | `/api/status` | Report session status + auto-stop missing sessions |
| POST | `/api/heartbeat` | Server heartbeat + alias sync + stale cleanup |

## Requirements

- **Bot Server**: Docker
- **Agent**: bash, curl, jq, `script` (util-linux), Claude Code CLI (`~/.local/bin/claude`)
