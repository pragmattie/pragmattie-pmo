from pathlib import Path

from alembic import command
from alembic.config import Config

API_DIR = Path(__file__).resolve().parents[1]


def test_migrations_upgrade_and_downgrade(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'migrate.db'}")
    get_settings.cache_clear()
    try:
        config = Config(str(API_DIR / "alembic.ini"))
        command.upgrade(config, "head")
        command.downgrade(config, "base")
    finally:
        get_settings.cache_clear()
