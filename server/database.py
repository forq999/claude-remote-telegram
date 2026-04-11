import json
import uuid
from datetime import datetime, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    name TEXT PRIMARY KEY,
    allowed_path TEXT DEFAULT '',
    aliases TEXT DEFAULT '{}',
    last_heartbeat TIMESTAMP,
    registered_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server TEXT NOT NULL,
    action TEXT NOT NULL,
    project_path TEXT,
    params TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    claim_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server TEXT NOT NULL,
    project_path TEXT NOT NULL,
    project_name TEXT,
    pid INTEGER,
    status TEXT DEFAULT 'running',
    idle_timeout INTEGER DEFAULT 1800,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMP,
    session_url TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    UNIQUE(server, project_path)
);
"""


async def init_db(db: aiosqlite.Connection):
    await db.executescript(SCHEMA)
    # 마이그레이션 (ALTER TABLE ADD COLUMN — 이미 있으면 예외 무시)
    migrations = [
        ("sessions", "session_url TEXT DEFAULT ''"),
        ("sessions", "session_id TEXT DEFAULT ''"),
        ("sessions", "display_name TEXT DEFAULT ''"),
        ("servers", "registered_at TIMESTAMP"),
        ("servers", "allowed_path TEXT DEFAULT ''"),
    ]
    for table, col in migrations:
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except Exception:
            pass
    # 백필: 기존 행들 중 session_id 가 UUID 형태가 아닌 것(= session_name 또는
    # 구 버그로 잘못 저장된 값) 을 display_name 으로 복사.
    # display_name 이 비어있을 때만 채우므로 재실행해도 멱등.
    # UUID 판정: 영숫자/대시로만 구성 + 36자 + 4-2-2-2-6 하이픈 포맷.
    #   SQLite LIKE 로 근사 매칭: "________-____-____-____-____________"
    await db.execute(
        """UPDATE sessions
           SET display_name = session_id
           WHERE display_name = ''
             AND session_id != ''
             AND NOT (
                 length(session_id) = 36
                 AND session_id LIKE '________-____-____-____-____________'
             )"""
    )
    await db.commit()


async def upsert_server(db, name, allowed_path, aliases):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO servers (name, allowed_path, aliases, last_heartbeat, registered_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
             allowed_path=excluded.allowed_path,
             aliases=excluded.aliases,
             last_heartbeat=excluded.last_heartbeat""",
        (name, allowed_path, json.dumps(aliases), now, now),
    )
    await db.commit()


async def get_server(db, name):
    cursor = await db.execute("SELECT * FROM servers WHERE name=?", (name,))
    return await cursor.fetchone()


async def get_all_servers(db):
    cursor = await db.execute("SELECT * FROM servers")
    return await cursor.fetchall()


async def create_command(db, server, action, project_path, params):
    now = datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        """INSERT INTO commands (server, action, project_path, params, status, updated_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (server, action, project_path, json.dumps(params), now),
    )
    await db.commit()
    return cursor.lastrowid


async def claim_commands(db, server):
    now = datetime.now(timezone.utc).isoformat()
    claim_id = str(uuid.uuid4())
    await db.execute(
        """UPDATE commands SET status='ack', updated_at=?, claim_id=?
           WHERE server=? AND status='pending'""",
        (now, claim_id, server),
    )
    await db.commit()
    cursor = await db.execute(
        "SELECT * FROM commands WHERE server=? AND claim_id=?",
        (server, claim_id),
    )
    return await cursor.fetchall()


async def complete_command(db, command_id, status, error=None):
    now = datetime.now(timezone.utc).isoformat()
    params = json.dumps({"error": error}) if error else None
    if params:
        await db.execute(
            "UPDATE commands SET status=?, params=?, updated_at=? WHERE id=?",
            (status, params, now, command_id),
        )
    else:
        await db.execute(
            "UPDATE commands SET status=?, updated_at=? WHERE id=?",
            (status, now, command_id),
        )
    await db.commit()
    cursor = await db.execute(
        "SELECT action, server, project_path FROM commands WHERE id=?",
        (command_id,),
    )
    return await cursor.fetchone()


async def upsert_session(db, server, project_path, project_name, pid, status,
                         idle_timeout=1800, session_url="", session_id="",
                         display_name=""):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO sessions (server, project_path, project_name, pid, status,
                                idle_timeout, started_at, last_activity, session_url,
                                session_id, display_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(server, project_path) DO UPDATE SET
             pid=excluded.pid, status=excluded.status,
             project_name=excluded.project_name,
             last_activity=excluded.last_activity,
             idle_timeout=excluded.idle_timeout,
             started_at=CASE WHEN sessions.status='stopped' THEN excluded.started_at ELSE sessions.started_at END,
             session_url=CASE WHEN excluded.session_url != '' THEN excluded.session_url ELSE sessions.session_url END,
             session_id=CASE WHEN excluded.session_id != '' THEN excluded.session_id ELSE sessions.session_id END,
             display_name=CASE WHEN sessions.display_name = '' AND excluded.display_name != '' THEN excluded.display_name ELSE sessions.display_name END""",
        (server, project_path, project_name, pid, status, idle_timeout, now, now,
         session_url, session_id, display_name),
    )
    await db.commit()


async def get_session_by_path(db, server, project_path):
    cursor = await db.execute(
        "SELECT * FROM sessions WHERE server=? AND project_path=?",
        (server, project_path),
    )
    return await cursor.fetchone()


async def get_running_session(db, server, project_path):
    cursor = await db.execute(
        "SELECT * FROM sessions WHERE server=? AND project_path=? AND status='running'",
        (server, project_path),
    )
    return await cursor.fetchone()


async def get_sessions(db, server=None):
    if server:
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE server=? AND status='running'",
            (server,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE status='running'"
        )
    return await cursor.fetchall()


async def stop_all_sessions(db, server):
    """Bulk-stop all running sessions for a server. Returns count."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        "UPDATE sessions SET status='stopped', last_activity=? WHERE server=? AND status='running'",
        (now, server),
    )
    await db.commit()
    return cursor.rowcount


async def stop_missing_sessions(db, server, reported_paths):
    now = datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        "SELECT project_path FROM sessions WHERE server=? AND status='running'",
        (server,),
    )
    rows = await cursor.fetchall()
    stopped = []
    for row in rows:
        if row["project_path"] not in reported_paths:
            await db.execute(
                "UPDATE sessions SET status='stopped', last_activity=? WHERE server=? AND project_path=? AND status='running'",
                (now, server, row["project_path"]),
            )
            stopped.append(row["project_path"])
    await db.commit()
    return stopped


async def get_stale_servers(db, stale_threshold_seconds=120):
    now = datetime.now(timezone.utc)
    cursor = await db.execute("SELECT name, last_heartbeat FROM servers")
    servers = await cursor.fetchall()
    stale_servers = []
    for s in servers:
        if s["last_heartbeat"]:
            hb = datetime.fromisoformat(s["last_heartbeat"])
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            diff = (now - hb).total_seconds()
            if diff > stale_threshold_seconds:
                # 2분 초과 → 서버 + 세션 삭제
                stale_servers.append(s["name"])
                await db.execute("DELETE FROM sessions WHERE server=?", (s["name"],))
                await db.execute("DELETE FROM servers WHERE name=?", (s["name"],))
    await db.commit()
    return stale_servers
