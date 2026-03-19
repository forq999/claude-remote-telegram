import json
import logging
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)

logger = logging.getLogger(__name__)


def parse_start_command(text: str):
    parts = text.split()
    if len(parts) < 3:
        return None
    return parts[1], " ".join(parts[2:])


def parse_stop_command(text: str):
    parts = text.split()
    if len(parts) < 2:
        return None
    server = parts[1]
    path = " ".join(parts[2:]) if len(parts) > 2 else None
    return server, path


def resolve_alias(alias_or_path: str, aliases: dict) -> str:
    return aliases.get(alias_or_path, alias_or_path)


def create_bot(token: str, admin_id: int, db_getter):
    def admin_only(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if update.effective_user is None or update.effective_user.id != admin_id:
                await update.message.reply_text("Unauthorized.")
                return
            return await func(update, context)
        return wrapper

    @admin_only
    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from server.database import (
            get_server, get_running_session, create_command,
        )
        parsed = parse_start_command(update.message.text)
        if not parsed:
            await update.message.reply_text(
                "Usage: /run <server> <path|alias>")
            return

        server_name, alias_or_path = parsed
        db = await db_getter()
        server = await get_server(db, server_name)
        if not server:
            await update.message.reply_text(f"Unknown server: {server_name}")
            return

        aliases = json.loads(server["aliases"] or "{}")
        project_path = resolve_alias(alias_or_path, aliases)

        existing = await get_running_session(db, server_name, project_path)
        if existing:
            await update.message.reply_text(
                f"Session already running: {server_name}:{project_path}")
            return

        cmd_id = await create_command(db, server_name, "start", project_path, {})
        await update.message.reply_text(
            f"Queued start: {server_name} @ {project_path} (cmd #{cmd_id})")

    @admin_only
    async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from server.database import get_server, create_command
        parsed = parse_stop_command(update.message.text)
        if not parsed:
            await update.message.reply_text("Usage: /stop <server> [path|alias]")
            return

        server_name, alias_or_path = parsed
        db = await db_getter()
        server = await get_server(db, server_name)
        if not server:
            await update.message.reply_text(f"Unknown server: {server_name}")
            return

        aliases = json.loads(server["aliases"] or "{}")
        project_path = resolve_alias(alias_or_path, aliases) if alias_or_path else None

        await create_command(db, server_name, "stop", project_path, {})
        target = project_path or "all sessions"
        await update.message.reply_text(f"Queued stop: {server_name} @ {target}")

    @admin_only
    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from server.database import get_sessions, get_all_servers, get_stale_servers
        db = await db_getter()
        parts = update.message.text.split()
        server_filter = parts[1] if len(parts) > 1 else None

        stale = await get_stale_servers(db)
        sessions = await get_sessions(db, server_filter)

        if not sessions:
            await update.message.reply_text("No active sessions.")
            return

        lines = []
        current_server = None
        for s in sessions:
            if s["server"] != current_server:
                current_server = s["server"]
                stale_mark = " (stale)" if current_server in stale else ""
                lines.append(f"\n{current_server}{stale_mark}")
            lines.append(
                f"  {s['project_name']} ({s['project_path']})")
            lines.append(
                f"    /stop {s['server']} {s['project_path']}")

        await update.message.reply_text("\n".join(lines))

    @admin_only
    async def cmd_servers(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from server.database import get_all_servers, get_stale_servers
        db = await db_getter()
        servers = await get_all_servers(db)
        stale = await get_stale_servers(db)

        if not servers:
            await update.message.reply_text("No registered servers.")
            return

        lines = []
        for s in servers:
            stale_mark = " (stale)" if s["name"] in stale else " (online)"
            aliases = json.loads(s["aliases"] or "{}")
            alias_str = ", ".join(f"{k}={v}" for k, v in aliases.items())
            lines.append(f"{s['name']}{stale_mark}")
            if alias_str:
                lines.append(f"  aliases: {alias_str}")

        await update.message.reply_text("\n".join(lines))

    @admin_only
    async def cmd_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from server.database import get_server, create_command
        parts = update.message.text.split()
        if len(parts) < 4:
            await update.message.reply_text(
                "Usage: /timeout <minutes> <server> <path|alias>")
            return

        try:
            minutes = int(parts[1])
            if minutes <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Minutes must be a positive integer.")
            return
        server_name = parts[2]
        alias_or_path = " ".join(parts[3:])

        db = await db_getter()
        server = await get_server(db, server_name)
        if not server:
            await update.message.reply_text(f"Unknown server: {server_name}")
            return

        aliases = json.loads(server["aliases"] or "{}")
        project_path = resolve_alias(alias_or_path, aliases)

        await create_command(
            db, server_name, "timeout", project_path,
            {"timeout_seconds": minutes * 60})
        await update.message.reply_text(
            f"Timeout -> {minutes}m: {server_name} @ {project_path}")

    @admin_only
    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "/run <server> <path|alias> - Start session\n"
            "/stop <server> [path|alias] - Stop session\n"
            "/status [server] - Show status\n"
            "/servers - List servers\n"
            "/timeout <min> <server> <path|alias> - Set timeout\n"
            "/help - This message"
        )
        await update.message.reply_text(text)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("run", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("servers", cmd_servers))
    app.add_handler(CommandHandler("timeout", cmd_timeout))
    app.add_handler(CommandHandler("help", cmd_help))

    return app
