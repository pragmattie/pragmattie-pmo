"""Application settings. Every setting lives here; secrets never appear in repr or logs."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PMO_", extra="ignore")

    app_name: str = "PragMattie PMO"
    database_url: str = Field(default="sqlite:///./test.db", repr=False)
    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
