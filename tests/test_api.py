import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from server.main import create_app
from server.config import Settings


@pytest.fixture
def test_settings(tmp_path):
    return Settings(
        telegram_bot_token="fake:token",
        telegram_admin_id=123,
        api_token="test-token",
        database_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token"}


@pytest_asyncio.fixture
async def client(test_settings):
    app = create_app(test_settings, start_bot=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_claim_no_commands(client, auth_headers):
    resp = await client.post("/api/commands/srv-a/claim", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["commands"] == []


@pytest.mark.asyncio
async def test_claim_unauthorized(client):
    resp = await client.post("/api/commands/srv-a/claim",
                             headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_registers_server(client, auth_headers):
    resp = await client.post("/api/heartbeat", headers=auth_headers, json={
        "server": "srv-a",
        "allowed_paths": ["/home/proj"],
        "aliases": {"front": "/home/proj/fe"},
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_status_report(client, auth_headers):
    await client.post("/api/heartbeat", headers=auth_headers, json={
        "server": "srv-a", "allowed_paths": [], "aliases": {},
    })
    resp = await client.post("/api/status", headers=auth_headers, json={
        "server": "srv-a",
        "sessions": [{
            "project_path": "/home/proj",
            "project_name": "proj",
            "pid": 9999,
            "status": "running",
            "idle_seconds": 30,
        }],
    })
    assert resp.status_code == 200
