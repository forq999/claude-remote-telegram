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
        db = await db_getter()
        await complete_command(db, command_id, body.status, body.error)
        return {"ok": True}

    @router.post("/heartbeat")
    async def heartbeat(body: HeartbeatRequest, _=Depends(auth)):
        from server.database import upsert_server
        db = await db_getter()
        await upsert_server(db, body.server, body.allowed_paths, body.aliases)
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
                s.pid, s.status,
            )
            if s.status == "running":
                reported_paths.add(s.project_path)
        # DB에 running인데 에이전트가 보고하지 않은 세션 → stopped
        stopped = await stop_missing_sessions(db, body.server, reported_paths)
        if stopped and notify_callback:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            for path in stopped:
                name = path.rstrip("/").rsplit("/", 1)[-1]
                run_data = f"run:{body.server}:{path}"
                markup = None
                if len(run_data) <= 64:
                    markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton("Restart", callback_data=run_data)
                    ]])
                await notify_callback(
                    f"*Stopped* `{body.server}` / `{name}`",
                    reply_markup=markup)
        return {"ok": True}

    return router
