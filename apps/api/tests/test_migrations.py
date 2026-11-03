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
            "fk_project_requests_project": "projects",
        }
        request_uniques = {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("project_requests")
        }
        assert request_uniques == {"uq_project_requests_project": ["project_id"]}
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
        readiness_fks = {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("readiness_checks")
        }
        assert readiness_fks == {
            "fk_readiness_checks_request": "project_requests",
            "fk_readiness_checks_confirmed_by": "users",
        }
        readiness_checks = {
            ck["name"] for ck in inspector.get_check_constraints("readiness_checks")
        }
        assert {
            "ck_readiness_checks_problem_rating",
            "ck_readiness_checks_approach_rating",
            "ck_readiness_checks_data_rating",
            "ck_readiness_checks_source",
        } <= readiness_checks
        readiness_uniques = {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("readiness_checks")
        }
        assert readiness_uniques == {"uq_readiness_checks_request": ["request_id"]}
        action_fks = {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("readiness_actions")
        }
        assert action_fks == {
            "fk_readiness_actions_check": "readiness_checks",
            "fk_readiness_actions_owner": "users",
        }
        action_uniques = {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("readiness_actions")
        }
        assert action_uniques == {"uq_readiness_actions_position": ["check_id", "area", "position"]}
        # 0013: an agent's draft action has no owner or date until a person adds them.
        action_columns = {c["name"]: c for c in inspector.get_columns("readiness_actions")}
        assert action_columns["owner_id"]["nullable"] is True
        assert action_columns["due_date"]["nullable"] is True
        gate_fks = {
            fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("gate_decisions")
        }
        assert gate_fks == {"fk_gate_decisions_decided_by": "users"}
        gate_checks = {ck["name"] for ck in inspector.get_check_constraints("gate_decisions")}
        assert {"ck_gate_decisions_outcome", "ck_gate_decisions_subject_type"} <= gate_checks
        gate_indexes = {ix["name"] for ix in inspector.get_indexes("gate_decisions")}
        assert "ix_gate_decisions_subject" in gate_indexes
        # 0014: a project made from a request names its sponsor and project manager as users.
        project_fks = {
            fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("projects")
        }
        assert project_fks == {
            "fk_projects_client": "clients",
            "fk_projects_sponsor_user": "users",
            "fk_projects_pm_user": "users",
        }
        # 0015: the brief, stakeholders and assumptions.
        brief_fks = {
            fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("project_briefs")
        }
        assert brief_fks == {"fk_project_briefs_project": "projects"}
        brief_uniques = {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("project_briefs")
        }
        assert brief_uniques == {"uq_project_briefs_project": ["project_id"]}
        assert {ck["name"] for ck in inspector.get_check_constraints("project_briefs")} == {
            "ck_project_briefs_budget"
        }
        stakeholder_fks = {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("project_stakeholders")
        }
        assert stakeholder_fks == {
            "fk_project_stakeholders_project": "projects",
            "fk_project_stakeholders_user": "users",
        }
        assumption_fks = {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("project_assumptions")
        }
        assert assumption_fks == {
            "fk_project_assumptions_project": "projects",
            "fk_project_assumptions_owner": "users",
        }
        assert {ck["name"] for ck in inspector.get_check_constraints("project_assumptions")} == {
            "ck_project_assumptions_status"
        }
        # Fails if the models and the migrations disagree.
        command.check(config)
        command.downgrade(config, "0013")
        inspector = inspect(create_engine(get_settings().database_url))
        assert "project_briefs" not in inspector.get_table_names()
        assert "project_id" not in {c["name"] for c in inspector.get_columns("project_requests")}
        assert "pm_user_id" not in {c["name"] for c in inspector.get_columns("projects")}
        command.downgrade(config, "0012")
        inspector = inspect(create_engine(get_settings().database_url))
        action_columns = {c["name"]: c for c in inspector.get_columns("readiness_actions")}
        assert action_columns["owner_id"]["nullable"] is False
        assert action_columns["due_date"]["nullable"] is False
        command.downgrade(config, "base")
    finally:
        get_settings.cache_clear()
