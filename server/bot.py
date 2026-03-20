import json
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
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


def fmt_duration(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


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
                "Usage: `/run <server> <path|alias>`",
                parse_mode=ParseMode.MARKDOWN)
            return

        server_name, alias_or_path = parsed
        db = await db_getter()
        server = await get_server(db, server_name)
        if not server:
            await update.message.reply_text(f"Server `{server_name}` not found.", parse_mode=ParseMode.MARKDOWN)
            return

        aliases = json.loads(server["aliases"] or "{}")
        project_path = resolve_alias(alias_or_path, aliases)

        existing = await get_running_session(db, server_name, project_path)
        if existing:
            await update.message.reply_text(
                f"Already running on `{server_name}`\n`{project_path}`",
                parse_mode=ParseMode.MARKDOWN)
            return

        await create_command(db, server_name, "start", project_path, {})

    @admin_only
    async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from server.database import get_server, create_command
        parsed = parse_stop_command(update.message.text)
        if not parsed:
            await update.message.reply_text(
                "Usage: `/stop <server> [path|alias]`",
                parse_mode=ParseMode.MARKDOWN)
            return

        server_name, alias_or_path = parsed
        db = await db_getter()
        server = await get_server(db, server_name)
        if not server:
            await update.message.reply_text(f"Server `{server_name}` not found.", parse_mode=ParseMode.MARKDOWN)
            return

        aliases = json.loads(server["aliases"] or "{}")
        project_path = resolve_alias(alias_or_path, aliases) if alias_or_path else None

        await create_command(db, server_name, "stop", project_path, {})

    @admin_only
    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from datetime import datetime, timezone
        from server.database import get_sessions, get_all_servers, get_stale_servers
        db = await db_getter()
        parts = update.message.text.split()
        server_filter = parts[1] if len(parts) > 1 else None

        stale = await get_stale_servers(db)
        sessions = await get_sessions(db, server_filter)

        if not sessions:
            await update.message.reply_text("No active sessions.")
            return

        now = datetime.now(timezone.utc)
        lines = []
        buttons = []
        current_server = None
        for s in sessions:
            if s["server"] != current_server:
                current_server = s["server"]
                if current_server in stale:
                    lines.append(f"\n*{current_server}* (offline)")
                else:
                    lines.append(f"\n*{current_server}*")

            # uptime
            uptime = "-"
            if s["started_at"]:
                started = datetime.fromisoformat(s["started_at"])
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                uptime = fmt_duration(int((now - started).total_seconds()))

            # idle
            idle_icon = ""
            idle_text = ""
            if s["last_activity"]:
                last_act = datetime.fromisoformat(s["last_activity"])
                if last_act.tzinfo is None:
                    last_act = last_act.replace(tzinfo=timezone.utc)
                idle_secs = int((now - last_act).total_seconds())
                if idle_secs < 60:
                    idle_icon = "active"
                elif idle_secs < 300:
                    idle_icon = "idle"
                    idle_text = fmt_duration(idle_secs)
                else:
                    idle_icon = "sleeping"
                    idle_text = fmt_duration(idle_secs)

            status_line = f"  `{s['project_name']}`"
            status_line += f"\n    Uptime: {uptime}"
            if idle_icon == "active":
                status_line += f" | Active"
            elif idle_text:
                status_line += f" | Idle: {idle_text}"
            session_url = s["session_url"] if s["session_url"] else None
            session_id = s["session_id"] if s["session_id"] else None
            if session_url:
                status_line += f"\n    [Open Session]({session_url})"
            if session_id:
                status_line += f"\n    ID: `{session_id}`"
            lines.append(status_line)

            callback_data = f"stop:{s['server']}:{s['project_path']}"
            if len(callback_data) <= 64:
                buttons.append([InlineKeyboardButton(
                    f"Stop {s['project_name']}", callback_data=callback_data)])
            else:
                buttons.append([InlineKeyboardButton(
                    f"Stop {s['project_name']}",
                    callback_data=f"stop:{s['server']}:{s['project_name']}")])

        markup = InlineKeyboardMarkup(buttons) if buttons else None
        await update.message.reply_text(
            "\n".join(lines), reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

    @admin_only
    async def cmd_servers(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from datetime import datetime, timezone
        from server.database import get_all_servers, get_stale_servers, get_sessions
        db = await db_getter()
        servers = await get_all_servers(db)
        stale = await get_stale_servers(db)

        if not servers:
            await update.message.reply_text("No registered servers.")
            return

        now = datetime.now(timezone.utc)
        lines = ["*Servers*\n"]
        buttons = []
        for s in servers:
            is_stale = s["name"] in stale
            icon = "+" if not is_stale else "-"
            status = "online" if not is_stale else "offline"

            # 마지막 heartbeat
            hb_text = ""
            if s["last_heartbeat"]:
                hb = datetime.fromisoformat(s["last_heartbeat"])
                if hb.tzinfo is None:
                    hb = hb.replace(tzinfo=timezone.utc)
                hb_ago = int((now - hb).total_seconds())
                hb_text = f" | {fmt_duration(hb_ago)} ago"

            # 업타임
            uptime_text = ""
            if not is_stale and s["registered_at"]:
                reg = datetime.fromisoformat(s["registered_at"])
                if reg.tzinfo is None:
                    reg = reg.replace(tzinfo=timezone.utc)
                uptime_text = f" | uptime {fmt_duration(int((now - reg).total_seconds()))}"

            # 활성 세션 수
            sessions = await get_sessions(db, s["name"])
            session_count = len(sessions)
            session_text = f"{session_count} active" if session_count > 0 else "no sessions"

            lines.append(f"[{icon}] *{s['name']}* ({status})")
            lines.append(f"    {session_text}{hb_text}{uptime_text}")

            aliases = json.loads(s["aliases"] or "{}")
            if aliases:
                lines.append("    Aliases:")
                for k, v in aliases.items():
                    lines.append(f"      `{k}` -> `{v}`")
                    run_data = f"run:{s['name']}:{k}"
                    if len(run_data) <= 64:
                        buttons.append([InlineKeyboardButton(
                            f"Run {s['name']} / {k}", callback_data=run_data)])
            lines.append("")

        markup = InlineKeyboardMarkup(buttons) if buttons else None
        await update.message.reply_text(
            "\n".join(lines), reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

    @admin_only
    async def cmd_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from server.database import get_server, create_command
        parts = update.message.text.split()
        if len(parts) < 4:
            await update.message.reply_text(
                "Usage: `/timeout <minutes> <server> <path|alias>`",
                parse_mode=ParseMode.MARKDOWN)
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
            await update.message.reply_text(f"Server `{server_name}` not found.", parse_mode=ParseMode.MARKDOWN)
            return

        aliases = json.loads(server["aliases"] or "{}")
        project_path = resolve_alias(alias_or_path, aliases)

        await create_command(
            db, server_name, "timeout", project_path,
            {"timeout_seconds": minutes * 60})
        await update.message.reply_text(
            f"Timeout updated to *{minutes}m*\n"
            f"Server: `{server_name}`\n"
            f"Path: `{project_path}`",
            parse_mode=ParseMode.MARKDOWN)

    @admin_only
    async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from server.database import create_command, get_all_servers, stop_all_sessions
        parts = update.message.text.split()
        if len(parts) < 2:
            db = await db_getter()
            servers = await get_all_servers(db)
            if not servers:
                await update.message.reply_text("No registered servers.")
                return
            buttons = []
            for s in servers:
                cb = f"clean:{s['name']}"
                if len(cb) <= 64:
                    buttons.append([InlineKeyboardButton(
                        f"Clean {s['name']}", callback_data=cb)])
            markup = InlineKeyboardMarkup(buttons) if buttons else None
            await update.message.reply_text(
                "*Select server to clean:*",
                parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            return
        server_name = parts[1]
        db = await db_getter()
        count = await stop_all_sessions(db, server_name)
        await create_command(db, server_name, "clean", None, {})
        await update.message.reply_text(
            f"*Clean* `{server_name}`\n"
            f"Sessions cleared: {count}\n"
            f"Cleanup queued",
            parse_mode=ParseMode.MARKDOWN)

    @admin_only
    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "*Claude Remote Control*\n"
            "\n"
            "*Session*\n"
            "`/run` `<server>` `<path|alias>`\n"
            "  Start a new remote session\n"
            "`/stop` `<server>` `[path|alias]`\n"
            "  Stop session (omit path for all)\n"
            "`/timeout` `<min>` `<server>` `<path|alias>`\n"
            "  Set idle timeout (default 30m)\n"
            "\n"
            "*Monitor*\n"
            "`/status` `[server]`\n"
            "  Active sessions, uptime, idle, session link\n"
            "`/servers`\n"
            "  Registered servers, aliases, run buttons\n"
            "\n"
            "*Manage*\n"
            "`/clean` `[server]`\n"
            "  Kill all sessions + cleanup (shows buttons if no arg)\n"
            "\n"
            "*Notes*\n"
            "  Stop -> Resume: resumes previous conversation\n"
            "  Stop -> New: starts fresh session\n"
            "  Servers -> Run: always new session\n"
            "  Sessions auto-stop after idle timeout\n"
            "  Servers auto-remove after 2min offline\n"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query.from_user.id != admin_id:
            await query.answer("Unauthorized.")
            return
        await query.answer()
        parts = query.data.split(":", 2)
        if len(parts) < 2:
            return
        action = parts[0]
        server_name = parts[1]
        path_or_name = parts[2] if len(parts) > 2 else None
        from server.database import get_server, get_running_session, create_command, stop_all_sessions
        db = await db_getter()
        server = await get_server(db, server_name)
        if not server:
            await query.edit_message_text(f"Server `{server_name}` not found.", parse_mode=ParseMode.MARKDOWN)
            return

        if action == "clean":
            count = await stop_all_sessions(db, server_name)
            await create_command(db, server_name, "clean", None, {})
            await query.edit_message_text(
                f"*Clean* `{server_name}`\n"
                f"Sessions cleared: {count}",
                parse_mode=ParseMode.MARKDOWN)
            return

        if not path_or_name:
            return
        aliases = json.loads(server["aliases"] or "{}")
        project_path = resolve_alias(path_or_name, aliases)

        if action == "stop":
            await create_command(db, server_name, "stop", project_path, {})
            await query.edit_message_text("Stopping...")

        elif action in ("run", "resume"):
            existing = await get_running_session(db, server_name, project_path)
            if existing:
                await query.edit_message_text(
                    f"Already running on `{server_name}` / `{project_path}`",
                    parse_mode=ParseMode.MARKDOWN)
                return
            params = {}
            if action == "resume":
                from server.database import get_session_by_path
                prev = await get_session_by_path(db, server_name, project_path)
                if prev and prev["session_id"]:
                    params = {"resume": prev["session_id"]}
            await create_command(db, server_name, "start", project_path, params)
            await query.edit_message_text(
                "Resuming..." if params.get("resume") else "Starting...")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("run", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("servers", cmd_servers))
    app.add_handler(CommandHandler("timeout", cmd_timeout))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(callback_handler))

    return app
