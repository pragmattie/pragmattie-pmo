"""Application settings. Every setting lives here; secrets never appear in repr or logs.

Settings come from the environment and from a `.env` file in the working directory. Outside
tests (`APP_ENV=test`), a missing database URL or JWT secret stops startup with a clear message.
A JWT secret shorter than 32 bytes stops startup in every environment; in tests a missing one is
replaced by a random secret made at runtime, so no secret is ever committed.
"""

import secrets
from functools import lru_cache
from pathlib import Path
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

# Our own floor (: HS256 keys shorter than this are too easy to guess.
JWT_SECRET_MIN_BYTES = 32


def _repo_root() -> Path:
    """The repository root (this file is `apps/api/app/config.py`). In a shallower tree, such as
    the API image's `/app/app/config.py`, this resolves to `/`."""
    parents = Path(__file__).resolve().parents
    return parents[min(3, len(parents) - 1)]


def _default_policies_dir() -> Path:
    return _repo_root() / "policies"


def _default_standards_map_path() -> Path:
    return _repo_root() / "docs" / "standards-map.yaml"


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
    jwt_ttl_minutes: int = Field(default=30, ge=1)
    # Session and CSRF cookies are Secure unless this is turned off. Turn it off only for local
    # development over plain http (docker-compose.yml does); never in a hosted environment.
    cookie_secure: bool = True

    # Argon2id cost parameters (argon2-cffi's defaults). Memory is in KiB.
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_cost: int = Field(default=65536, ge=8)
    argon2_parallelism: int = Field(default=4, ge=1)
    anthropic_api_key: str | None = Field(default=None, repr=False)

    cors_origins: str = "http://localhost:5173"
    agent_mode: AgentMode = "off"

    # Governance policy files; read only by app/policy.py, on first use.
    policies_dir: Path = Field(default_factory=_default_policies_dir)
    # Standards traceability map; read only by app/standards.py, on first use.
    standards_map_path: Path = Field(default_factory=_default_standards_map_path)

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
        if self.jwt_secret is not None and len(self.jwt_secret.encode()) < JWT_SECRET_MIN_BYTES:
            raise ValueError(f"JWT_SECRET must be at least {JWT_SECRET_MIN_BYTES} bytes long.")
        if self.app_env == "test":
            if not self.database_url:
                self.database_url = TEST_DATABASE_URL
            if not self.jwt_secret:
                self.jwt_secret = secrets.token_urlsafe(48)
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
