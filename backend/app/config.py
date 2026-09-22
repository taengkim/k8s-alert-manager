from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAM_")

    database_url: str = "sqlite+aiosqlite:///./kam.db"
    app_name: str = "kam"


@lru_cache
def get_settings() -> Settings:
    return Settings()
