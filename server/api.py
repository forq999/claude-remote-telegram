import json
import re

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel


# Claude Code jsonl 파일명(=agent 의 session_id) 포맷. 이 형태가 아닌 값은
# 에이전트가 일시적으로 보고하는 session_name (예: bk-test_proj_YYMMDD_HH-MM-SS)
# 으로 간주하고 display_name 컬럼에 복사한다.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def get_auth_checker(api_token: str):
    async def check_auth(authorization: str = Header()):
        if authorization != f"Bearer {api_token}":
            raise HTTPException(status_code=401, detail="Unauthorized")
    return check_auth


class HeartbeatRequest(BaseModel):
    server: str
    allowed_path: str = ""
    allowed_paths: list[str] | None = None  # deprecated, 하위호환용
    aliases: dict[str, str]


class SessionReport(BaseModel):
    project_path: str
    project_name: str
    pid: int
    status: str
    idle_seconds: int
    session_url: str = ""
    session_id: str = ""
    display_name: str = ""


class StatusRequest(BaseModel):
    server: str
    sessions: list[SessionReport]


class CommandDoneRequest(BaseModel):
    status: str
    error: str | None = None


def path_basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] if path else ""


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
            name = path_basename(path)
            if body.status == "done":
                if action == "start":
                    from server.database import get_session_by_path
                    sess = await get_session_by_path(db, server, path)
                    session_url = sess["session_url"] if sess and sess["session_url"] else None
                    # display_name 우선 (사람이 읽기 쉬운 이름), 없으면 session_id(UUID) 폴백
                    label = None
                    if sess:
                        label = (sess["display_name"] if sess["display_name"]
                                 else sess["session_id"]) or None

                    stop_data = f"stop:{server}:{path}"
                    btns = []
                    if len(stop_data) <= 64:
                        btns.append(InlineKeyboardButton("Stop", callback_data=stop_data))
                    if session_url:
                        btns.append(InlineKeyboardButton("Open", url=session_url))
                    markup = InlineKeyboardMarkup([btns]) if btns else None

                    msg = f"*Started* `{server}` / `{name}`"
                    if label:
                        msg += f"\nID: `{label}`"
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
        path = body.allowed_path or (body.allowed_paths[0] if body.allowed_paths else "")
        await upsert_server(db, body.server, path, body.aliases)
        await get_stale_servers(db)
        return {"ok": True}

    @router.post("/status")
    async def status_report(body: StatusRequest, _=Depends(auth)):
        from server.database import (
            upsert_session, stop_missing_sessions, get_running_session,
        )
        db = await db_getter()
        # 에이전트가 보고한 running 경로 목록
        reported_paths = set()
        agent_stopped = []
        for s in body.sessions:
            # running→stopped 전환 감지 (upsert 전에 확인)
            if s.status == "stopped":
                prev = await get_running_session(db, body.server, s.project_path)
                if prev:
                    agent_stopped.append(s.project_path)
            # display_name 결정 우선순위:
            #  1) 에이전트가 jsonl 의 customTitle 에서 직접 추출해 보고한 값
            #     (report_status 가 UUID 확정된 세션에 대해 채움)
            #  2) session_id 필드가 아직 UUID 가 아닌 transient 상태
            #     (start_session 직후 초기 session_name 보고)
            # upsert_session 의 CASE 로직이 최초 non-empty 값만 저장하므로
            # 이후 빈 값 보고가 와도 덮이지 않음. 또한 같은 값을 매 사이클
            # 재보고해도 idempotent.
            sid = s.session_id or ""
            display_name = s.display_name or ""
            if not display_name and not _UUID_RE.match(sid):
                display_name = sid
            await upsert_session(
                db, body.server, s.project_path, s.project_name,
                s.pid, s.status, session_url=s.session_url,
                session_id=s.session_id, display_name=display_name,
            )
            if s.status == "running":
                reported_paths.add(s.project_path)
        # DB에 running인데 에이전트가 보고하지 않은 세션 → stopped
        stopped = await stop_missing_sessions(db, body.server, reported_paths)
        all_stopped = stopped + agent_stopped
        if all_stopped and notify_callback:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            for path in all_stopped:
                name = path_basename(path)
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
