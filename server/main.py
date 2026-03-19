import aiosqlite
from contextlib import asynccontextmanager
from fastapi import FastAPI

from server.config import Settings
from server.api import create_api_router
from server.bot import create_bot
from server.database import init_db


def create_app(app_settings: Settings = None, start_bot: bool = True) -> FastAPI:
    s = app_settings
    db_holder = {"conn": None}
    bot_holder = {"app": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_holder["conn"] = await aiosqlite.connect(s.database_path)
        db_holder["conn"].row_factory = aiosqlite.Row
        await init_db(db_holder["conn"])

        if start_bot:
            bot_holder["app"] = create_bot(s.telegram_bot_token, s.telegram_admin_id, get_db)
            await bot_holder["app"].initialize()
            await bot_holder["app"].start()
            await bot_holder["app"].updater.start_polling()

        yield

        if bot_holder["app"]:
            await bot_holder["app"].updater.stop()
            await bot_holder["app"].stop()
            await bot_holder["app"].shutdown()
        await db_holder["conn"].close()

    async def get_db():
        return db_holder["conn"]

    async def notify(message: str, reply_markup=None):
        if bot_holder["app"]:
            await bot_holder["app"].bot.send_message(
                chat_id=s.telegram_admin_id, text=message,
                parse_mode="Markdown", reply_markup=reply_markup)

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_api_router(get_db, s.api_token, notify_callback=notify))
    return app


settings = Settings()
app = create_app(settings)
