from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

API_DIR = Path(__file__).resolve().parents[1]


def test_migrations_upgrade_check_and_downgrade(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'migrate.db'}")
    get_settings.cache_clear()
    try:
        config = Config(str(API_DIR / "alembic.ini"))
        command.upgrade(config, "head")
        inspector = inspect(create_engine(get_settings().database_url))
        assert {"users", "revoked_tokens"} <= set(inspector.get_table_names())
        actor_fks = [
            fk
            for fk in inspector.get_foreign_keys("pmo_audit")
            if fk["constrained_columns"] == ["actor_user_id"]
        ]
        assert [(fk["name"], fk["referred_table"], fk["referred_columns"]) for fk in actor_fks] == [
            ("fk_pmo_audit_actor_user", "users", ["id"])
        ]
        checks = {ck["name"] for ck in inspector.get_check_constraints("pmo_audit")}
        assert {"ck_pmo_audit_trigger", "ck_pmo_audit_status"} <= checks
        user_columns = {c["name"] for c in inspector.get_columns("users")}
        assert "failed_window_started_at" in user_columns
        # Fails if the models and the migrations disagree.
        command.check(config)
        command.downgrade(config, "base")
    finally:
        get_settings.cache_clear()
