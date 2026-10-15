from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telethon_api_id: int = 0
    telethon_api_hash: str = ""
    telethon_string_session: str = ""
    telegram_bot_token: str = ""
    
    monitored_channels: list[str] = ["monitoring_zaxid", "kpszsu", "kudy_letyt", "monitorwarr"]

    city_profile: str = "ivano-frankivsk"
    profiles_dir: str = "data/profiles"

    reference_dir: str = "data/reference"
    learning_db_path: str = "data/learning.db"
    subscribers_db_path: str = "data/subscribers.db"
    
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    siren_enabled: bool = True
    # Empty value: use the region from the city profile
    siren_region: str = ""
    siren_poll_interval_seconds: float = 12.0
    siren_sources: list[str] = [
        "https://ubilling.net.ua/aerialalerts/",
        "https://ubilling.net.ua/aerialalerts/?source=jaam",
        "https://ubilling.net.ua/aerialalerts/?source=skog",
        "https://ubilling.net.ua/aerialalerts/?source=klimenko",
        "http://ubilling.net.ua/aerialalerts/",
    ]

    # Feedback settings
    feedback_chat_id: int | None = None
    feedback_admin_ids: list[int] | str = []

    @field_validator("feedback_admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                import json
                return json.loads(v)
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
