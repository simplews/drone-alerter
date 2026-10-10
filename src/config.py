from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    telethon_api_id: int = 0
    telethon_api_hash: str = ""
    telethon_string_session: str = ""
    telegram_bot_token: str = ""
    database_url: str = "sqlite:///app.db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
