import json

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel


def get_auth_checker(api_token: str):
    async def check_auth(authorization: str = Header()):
        if authorization != f"Bearer {api_token}":
            raise HTTPException(status_code=401, detail="Unauthorized")
    return check_auth


class HeartbeatRequest(BaseModel):
    server: str
    allowed_paths: list[str]
    aliases: dict[str, str]


class SessionReport(BaseModel):
    project_path: str
    project_name: str
    pid: int
    status: str
    idle_seconds: int
    session_url: str = ""
    session_id: str = ""


class StatusRequest(BaseModel):
    server: str
    sessions: list[SessionReport]


class CommandDoneRequest(BaseModel):
    status: str
    error: str | None = None


def create_api_router(db_getter, api_token: str, notify_callback=None) -> APIRouter:
    router = APIRouter(prefix="/api")
    auth = get_auth_checker(api_token)

    @router.post("/commands/{server}/claim")
    async def claim_commands(server: str, _=Depends(auth)):
        from server.database import claim_commands as db_claim
        db = await db_getter()
        rows = await db_claim(db, server)
        commands = [
            {"id": r["id"], "action": r["action"],
             "project_path": r["project_path"],
             "params": json.loads(r["params"] or "{}")}
            for r in rows
        ]
        return {"commands": commands}

    @router.post("/commands/{command_id}/done")
    async def command_done(command_id: int, body: CommandDoneRequest,
                           _=Depends(auth)):
        from server.database import complete_command
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        db = await db_getter()
        cmd = await complete_command(db, command_id, body.status, body.error)
        if cmd and notify_callback:
            action = cmd["action"]
            server = cmd["server"]
            path = cmd["project_path"] or ""
            name = path.rstrip("/").rsplit("/", 1)[-1] if path else ""
            if body.status == "done":
                if action == "start":
                    from server.database import get_session_by_path
                    sess = await get_session_by_path(db, server, path)
                    session_url = sess["session_url"] if sess and sess["session_url"] else None
                    session_id = sess["session_id"] if sess and sess["session_id"] else None

                    stop_data = f"stop:{server}:{path}"
                    btns = []
                    if len(stop_data) <= 64:
                        btns.append(InlineKeyboardButton("Stop", callback_data=stop_data))
                    if session_url:
                        btns.append(InlineKeyboardButton("Open", url=session_url))
                    markup = InlineKeyboardMarkup([btns]) if btns else None

                    msg = f"*Started* `{server}` / `{name}`"
                    if session_id:
                        msg += f"\nID: `{session_id}`"
                    if session_url:
                        msg += f"\n[Open Session]({session_url})"
                    await notify_callback(msg, reply_markup=markup)
                elif action == "stop":
                    pass  # stop_missing_sessions에서 알림
                elif action == "clean":
                    await notify_callback(
                        f"*Cleaned* `{server}`")
            elif body.status == "failed":
                await notify_callback(
                    f"*Failed* `{server}` / `{name}`\n{body.error or ''}")
        return {"ok": True}

    @router.post("/heartbeat")
    async def heartbeat(body: HeartbeatRequest, _=Depends(auth)):
        from server.database import upsert_server, get_stale_servers
        db = await db_getter()
        await upsert_server(db, body.server, body.allowed_paths, body.aliases)
        await get_stale_servers(db)
        return {"ok": True}

    @router.post("/status")
    async def status_report(body: StatusRequest, _=Depends(auth)):
        from server.database import upsert_session, stop_missing_sessions
        db = await db_getter()
        # 에이전트가 보고한 running 경로 목록
        reported_paths = set()
        for s in body.sessions:
            await upsert_session(
                db, body.server, s.project_path, s.project_name,
                s.pid, s.status, session_url=s.session_url,
                session_id=s.session_id,
            )
            if s.status == "running":
                reported_paths.add(s.project_path)
        # DB에 running인데 에이전트가 보고하지 않은 세션 → stopped
        stopped = await stop_missing_sessions(db, body.server, reported_paths)
        if stopped and notify_callback:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            for path in stopped:
                name = path.rstrip("/").rsplit("/", 1)[-1]
                resume_data = f"resume:{body.server}:{path}"
                markup = None
                if len(resume_data) <= 64:
                    markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton("Resume", callback_data=resume_data),
                        InlineKeyboardButton("New", callback_data=f"run:{body.server}:{path}")
                    ] if len(f"run:{body.server}:{path}") <= 64 else [
                        InlineKeyboardButton("Resume", callback_data=resume_data)
                    ]])
                await notify_callback(
                    f"*Stopped* `{body.server}` / `{name}`",
                    reply_markup=markup)
        return {"ok": True}

    return router
