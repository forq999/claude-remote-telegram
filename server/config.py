from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_admin_id: int
    api_token: str
    database_path: str = "/data/claude-remote.db"
    host: str = "0.0.0.0"
    port: int = 8443

    model_config = {"env_file": ".env"}
