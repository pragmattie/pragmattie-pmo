"""Application settings. Every setting lives here; secrets never appear in repr or logs.

Settings come from the environment and from a `.env` file in the working directory. Outside
tests (`APP_ENV=test`), a missing database URL or JWT secret stops startup with a clear message.
"""

from functools import lru_cache
from typing import Literal, get_args

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

AppEnv = Literal["dev", "test", "prod"]
AgentMode = Literal["off", "shadow", "enforce"]

# Closed value sets: a tuple constant for code, the Literal above for validation.
APP_ENVS: tuple[AppEnv, ...] = get_args(AppEnv)
AGENT_MODES: tuple[AgentMode, ...] = get_args(AgentMode)

TEST_DATABASE_URL = "sqlite:///./test.db"


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error must never echo a password or secret.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_name: str = "PragMattie PMO"
    app_env: AppEnv = "dev"

    # Either a full DATABASE_URL, or the separate DB_* parts (the password is URL-encoded).
    database_url: str | None = Field(default=None, repr=False)
    db_host: str | None = None
    db_port: int = 3306
    db_user: str | None = None
    db_password: str | None = Field(default=None, repr=False)
    db_name: str = "pragmattie_pmo"
    # CA certificate for TLS to the hosted database: a file path or the PEM text itself.
    database_ca_cert: str | None = Field(default=None, repr=False)

    jwt_secret: str | None = Field(default=None, repr=False)
    anthropic_api_key: str | None = Field(default=None, repr=False)

    cors_origins: str = "http://localhost:5173"
    agent_mode: AgentMode = "off"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _resolve_and_require(self) -> "Settings":
        if not self.database_url and self.db_host:
            self.database_url = URL.create(
                "mysql+pymysql",
                username=self.db_user,
                password=self.db_password,
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
            ).render_as_string(hide_password=False)
        if self.app_env == "test":
            if not self.database_url:
                self.database_url = TEST_DATABASE_URL
            return self
        missing = []
        if not self.database_url:
            missing.append("DATABASE_URL (or DB_HOST, DB_USER, DB_PASSWORD, DB_NAME)")
        if not self.jwt_secret:
            missing.append("JWT_SECRET")
        if missing:
            raise ValueError(
                "missing required settings: "
                + "; ".join(missing)
                + ". Set them in the environment or in .env (see .env.example)."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
