"""Database engine and session factory (SQLAlchemy 2).

Works on SQLite (tests), local MySQL and the hosted MySQL. When DATABASE_CA_CERT is set, MySQL
connections use TLS and verify the server against that CA.
"""

import tempfile
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings


@lru_cache
def _pem_file(pem: str) -> str:
    """Write PEM text to a temporary file once, since the driver wants a path."""
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as handle:
        handle.write(pem)
    return handle.name


def ca_cert_path(value: str) -> str:
    """DATABASE_CA_CERT holds either a path or the PEM text itself."""
    if value.lstrip().startswith("-----BEGIN"):
        return _pem_file(value.strip() + "\n")
    return value


def connect_args(settings: Settings) -> dict[str, Any]:
    url = make_url(settings.database_url or "")
    if url.get_backend_name() == "mysql" and settings.database_ca_cert:
        return {"ssl": {"ca": ca_cert_path(settings.database_ca_cert)}}
    return {}


def make_engine(settings: Settings, **kwargs: Any) -> Engine:
    return create_engine(
        settings.database_url or "",
        pool_pre_ping=True,
        connect_args=connect_args(settings),
        **kwargs,
    )


engine = make_engine(get_settings())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    with SessionLocal() as session:
        yield session
