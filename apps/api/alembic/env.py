"""Alembic environment: the URL and TLS options come from the application settings."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from app.config import get_settings
from app.db import make_engine
from app.migrations_filter import VERSION_TABLE, include_name
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_options = {
    "target_metadata": target_metadata,
    "compare_type": True,
    "include_name": include_name,
    "version_table": VERSION_TABLE,
}


def run_migrations_offline() -> None:
    context.configure(url=get_settings().database_url, literal_binds=True, **_options)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = make_engine(get_settings(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, **_options)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
