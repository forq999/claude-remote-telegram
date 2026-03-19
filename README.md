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
- **Agent**: Single bash script per server, runs via cron every 10s

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
# Edit .env with your values:
#   TELEGRAM_BOT_TOKEN=123456:ABC...
#   TELEGRAM_ADMIN_ID=987654321
#   API_TOKEN=your-shared-secret

docker compose up --build -d
```

### 3. Agent (Each Server)

```bash
# Copy agent script
cp agent/claude-agent.sh /home/user/claude-agent.sh
chmod +x /home/user/claude-agent.sh
```

Edit the config section at the top of the script:

```bash
SERVER_NAME="my-server"
BOT_API_URL="http://your-bot-server:8443"
API_TOKEN="your-shared-secret"        # same as .env
DEFAULT_TIMEOUT=1800                   # 30 min idle timeout
ALLOWED_PATHS="/home/user/projects"
ALIASES="front=/home/user/projects/frontend,api=/home/user/projects/backend"
```

Register in cron (10s interval):

```bash
# /etc/crontab (with user field)
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
| `/run <server> <path\|alias>` | Start a remote session |
| `/stop <server> [path\|alias]` | Stop session (omit path for all) |
| `/status [server]` | Show active sessions with uptime and idle status |
| `/servers` | List registered servers and aliases |
| `/timeout <min> <server> <path\|alias>` | Change idle timeout |
| `/clean <server>` | Kill all sessions and cleanup |
| `/help` | Show help |

## Features

- Multi-server, multi-project session management
- Path aliases for quick access (`/run 69 ssh` instead of full path)
- Auto-shutdown on idle (configurable timeout, default 30min)
- Inline buttons for one-tap stop/restart
- Real-time notifications on session start/stop/fail
- Automatic stale session cleanup
- `flock` prevents concurrent agent execution
- Path validation (prefix matching, traversal prevention, shell metachar blocking)

## API Endpoints

All endpoints require `Authorization: Bearer {API_TOKEN}` header.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/commands/{server}/claim` | Atomically claim pending commands |
| POST | `/api/commands/{id}/done` | Report command completion |
| POST | `/api/status` | Report session status |
| POST | `/api/heartbeat` | Server heartbeat + alias sync |

## Requirements

- **Bot Server**: Docker
- **Agent**: bash, curl, jq, Claude Code CLI (`~/.local/bin/claude`)
