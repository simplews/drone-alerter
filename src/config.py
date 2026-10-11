from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telethon_api_id: int = 0
    telethon_api_hash: str = ""
    telethon_string_session: str = ""
    telegram_bot_token: str = ""
    
    monitored_channels: list[str] = ["monitoring_zaxid", "kudy_letyt", "monitorwarr"]
    reference_dir: str = "data/reference"
    learning_db_path: str = "data/learning.db"
    subscribers_db_path: str = "data/subscribers.db"
    
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    siren_enabled: bool = True
    siren_region: str = "Івано-Франківська область"
    siren_poll_interval_seconds: float = 12.0
    siren_sources: list[str] = [
        "https://ubilling.net.ua/aerialalerts/",
        "https://ubilling.net.ua/aerialalerts/?source=jaam",
        "https://ubilling.net.ua/aerialalerts/?source=skog",
        "https://ubilling.net.ua/aerialalerts/?source=klimenko",
        "http://ubilling.net.ua/aerialalerts/",
    ]

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
