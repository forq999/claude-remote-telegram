import pytest
from server.database import (
    init_db, upsert_server, get_server,
    create_command, claim_commands, complete_command,
    upsert_session, get_sessions, get_running_session,
)


@pytest.mark.asyncio
async def test_upsert_and_get_server(db):
    await upsert_server(db, "srv-a", ["/home/proj"], {"front": "/home/proj/fe"})
    row = await get_server(db, "srv-a")
    assert row["name"] == "srv-a"


@pytest.mark.asyncio
async def test_create_and_claim_command(db):
    await upsert_server(db, "srv-a", [], {})
    cmd_id = await create_command(db, "srv-a", "start", "/home/proj", {})
    claimed = await claim_commands(db, "srv-a")
    assert len(claimed) == 1
    assert claimed[0]["id"] == cmd_id
    assert claimed[0]["status"] == "ack"
    again = await claim_commands(db, "srv-a")
    assert len(again) == 0


@pytest.mark.asyncio
async def test_complete_command(db):
    await upsert_server(db, "srv-a", [], {})
    cmd_id = await create_command(db, "srv-a", "start", "/home/proj", {})
    await claim_commands(db, "srv-a")
    await complete_command(db, cmd_id, "done")


@pytest.mark.asyncio
async def test_duplicate_running_session_check(db):
    await upsert_server(db, "srv-a", [], {})
    await upsert_session(db, "srv-a", "/home/proj", "proj", 1234, "running")
    existing = await get_running_session(db, "srv-a", "/home/proj")
    assert existing is not None
    assert existing["pid"] == 1234


@pytest.mark.asyncio
async def test_get_sessions_by_server(db):
    await upsert_server(db, "srv-a", [], {})
    await upsert_session(db, "srv-a", "/p1", "p1", 100, "running")
    await upsert_session(db, "srv-a", "/p2", "p2", 200, "running")
    sessions = await get_sessions(db, "srv-a")
    assert len(sessions) == 2
