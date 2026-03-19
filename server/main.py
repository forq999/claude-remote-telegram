import aiosqlite
from contextlib import asynccontextmanager
from fastapi import FastAPI

from server.config import Settings
from server.api import create_api_router
from server.database import init_db


def create_app(app_settings: Settings = None, start_bot: bool = True) -> FastAPI:
    s = app_settings
    db_holder = {"conn": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_holder["conn"] = await aiosqlite.connect(s.database_path)
        db_holder["conn"].row_factory = aiosqlite.Row
        await init_db(db_holder["conn"])

        if start_bot:
            pass  # Task 5에서 구현

        yield

        await db_holder["conn"].close()

    async def get_db():
        return db_holder["conn"]

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_api_router(get_db, s.api_token))
    return app
