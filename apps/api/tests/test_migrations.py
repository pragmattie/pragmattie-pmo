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
        risk_checks = {ck["name"] for ck in inspector.get_check_constraints("risks")}
        assert {"ck_risks_kind", "ck_risks_probability", "ck_risks_score"} <= risk_checks
        risk_fks = {fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("risks")}
        assert risk_fks == {"fk_risks_project": "projects", "fk_risks_owner_user": "users"}
        risk_indexes = {ix["name"] for ix in inspector.get_indexes("risks")}
        assert {"ix_risks_project_status", "ix_risks_band", "ix_risks_score"} <= risk_indexes
        risk_uniques = {
            uq["name"]: uq["column_names"] for uq in inspector.get_unique_constraints("risks")
        }
        assert risk_uniques == {"uq_risks_seed_key": ["seed_key"]}
        # Risk ids are never reused, so their audit rows can't be mixed up.
        with create_engine(get_settings().database_url).connect() as conn:
            risks_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = 'risks'"
            ).scalar_one()
        assert "AUTOINCREMENT" in risks_sql
        request_fks = {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("project_requests")
        }
        assert request_fks == {
            "fk_project_requests_client": "clients",
            "fk_project_requests_requested_by": "users",
        }
        request_indexes = {ix["name"] for ix in inspector.get_indexes("project_requests")}
        assert {
            "ix_project_requests_status",
            "ix_project_requests_submitted_at",
            "ix_project_requests_wanted_by",
        } <= request_indexes
        request_checks = {ck["name"] for ck in inspector.get_check_constraints("project_requests")}
        assert {"ck_project_requests_status", "ck_project_requests_budget"} <= request_checks
        with create_engine(get_settings().database_url).connect() as conn:
            requests_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = 'project_requests'"
            ).scalar_one()
        assert "AUTOINCREMENT" in requests_sql
        value_case_fks = {
            fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("value_cases")
        }
        assert value_case_fks == {
            "fk_value_cases_request": "project_requests",
            "fk_value_cases_confirmed_by": "users",
        }
        value_case_checks = {ck["name"] for ck in inspector.get_check_constraints("value_cases")}
        assert {"ck_value_cases_cost", "ck_value_cases_source"} <= value_case_checks
        value_case_uniques = {
            uq["name"]: uq["column_names"] for uq in inspector.get_unique_constraints("value_cases")
        }
        assert value_case_uniques == {"uq_value_cases_request": ["request_id"]}
        benefit_uniques = {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("value_case_benefits")
        }
        assert benefit_uniques == {"uq_value_case_benefits_position": ["value_case_id", "position"]}
        # Fails if the models and the migrations disagree.
        command.check(config)
        command.downgrade(config, "base")
    finally:
        get_settings.cache_clear()
