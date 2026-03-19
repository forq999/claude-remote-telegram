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

1. Message `@BotFather` on Telegram
2. `/newbot` -> set name and username
3. Copy the bot token
4. `/setcommands` -> paste:

```
run - Start session (server path|alias)
stop - Stop session (server [path|alias])
status - Show active sessions
servers - List registered servers
timeout - Set idle timeout (min server path|alias)
clean - Kill all sessions and cleanup (server)
help - Show help message
```

5. Get your user ID from `@userinfobot`

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
