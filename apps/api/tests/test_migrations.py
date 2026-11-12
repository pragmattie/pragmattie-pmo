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
        # Project ids are never reused either, so a demo reset can't mix up audit rows (0025).
        with create_engine(get_settings().database_url).connect() as conn:
            projects_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = 'projects'"
            ).scalar_one()
        assert "AUTOINCREMENT" in projects_sql
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
        assert request_uniques == {
            "uq_project_requests_project": ["project_id"],
            "uq_project_requests_seed_key": ["seed_key"],
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
        # 0016: the plan, its milestones, deliverables and estimate lines.
        plan_fks = {
            fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("project_plans")
        }
        assert plan_fks == {"fk_project_plans_project": "projects"}
        plan_uniques = {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("project_plans")
        }
        assert plan_uniques == {"uq_project_plans_project": ["project_id"]}
        assert {ck["name"] for ck in inspector.get_check_constraints("project_plans")} == {
            "ck_project_plans_contingency",
            "ck_project_plans_report_period",
            "ck_project_plans_report_due_day",
        }
        assert {
            fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("plan_milestones")
        } == {"fk_plan_milestones_plan": "project_plans", "fk_plan_milestones_owner": "users"}
        assert {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("plan_deliverables")
        } == {
            "fk_plan_deliverables_plan": "project_plans",
            "fk_plan_deliverables_milestone": "plan_milestones",
        }
        assert {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("plan_estimate_lines")
        } == {"fk_plan_estimate_lines_plan": "project_plans"}
        assert {ck["name"] for ck in inspector.get_check_constraints("plan_estimate_lines")} == {
            "ck_plan_estimate_lines_category",
            "ck_plan_estimate_lines_amount",
            "ck_plan_estimate_lines_source",
        }
        with create_engine(get_settings().database_url).connect() as conn:
            for table in ("plan_milestones", "plan_deliverables", "plan_estimate_lines"):
                table_sql = conn.exec_driver_sql(
                    f"SELECT sql FROM sqlite_master WHERE name = '{table}'"
                ).scalar_one()
                assert "AUTOINCREMENT" in table_sql, table
        # 0017: the proposal, its frozen versions and events, and the risk's tick.
        assert {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("project_proposals")
        } == {
            "fk_project_proposals_project": "projects",
            "fk_project_proposals_created_by": "users",
        }
        assert {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("project_proposals")
        } == {"uq_project_proposals_project": ["project_id"]}
        assert {ck["name"] for ck in inspector.get_check_constraints("project_proposals")} == {
            "ck_project_proposals_status"
        }
        assert {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("proposal_versions")
        } == {
            "fk_proposal_versions_proposal": "project_proposals",
            "fk_proposal_versions_submitted_by": "users",
        }
        assert {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("proposal_versions")
        } == {"uq_proposal_versions_number": ["proposal_id", "number"]}
        assert {
            fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("proposal_events")
        } == {
            "fk_proposal_events_proposal": "project_proposals",
            "fk_proposal_events_version": "proposal_versions",
            "fk_proposal_events_actor": "users",
        }
        assert {ck["name"] for ck in inspector.get_check_constraints("proposal_events")} == {
            "ck_proposal_events_kind",
            "ck_proposal_events_answer",
            # 0024
            "ck_proposal_events_source",
            "ck_proposal_events_actor",
        }
        risk_columns = {c["name"]: c for c in inspector.get_columns("risks")}
        assert risk_columns["show_in_proposal"]["nullable"] is False
        # 0018: who wrote each proposal section.
        proposal_columns = {c["name"]: c for c in inspector.get_columns("project_proposals")}
        assert proposal_columns["section_sources"]["nullable"] is False
        # 0019: the go-ahead gate's baselines, and a project's version and status values.
        project_columns = {c["name"]: c for c in inspector.get_columns("projects")}
        assert project_columns["version"]["nullable"] is False
        assert "ck_projects_status" in {
            ck["name"] for ck in inspector.get_check_constraints("projects")
        }
        assert {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("project_baselines")
        } == {
            "fk_project_baselines_project": "projects",
            "fk_project_baselines_gate_decision": "gate_decisions",
            "fk_project_baselines_change_request": "change_requests",
        }
        assert {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("project_baselines")
        } == {
            "uq_project_baselines_number": ["project_id", "number"],
            "uq_project_baselines_change_request": ["change_request_id"],
        }
        # 0020: change requests, and a baseline made by a gate decision or a change request.
        assert {
            fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("change_requests")
        } == {
            "fk_change_requests_project": "projects",
            "fk_change_requests_raised_by": "users",
            "fk_change_requests_decided_by": "users",
        }
        assert {ck["name"] for ck in inspector.get_check_constraints("change_requests")} == {
            "ck_change_requests_status",
            "ck_change_requests_decider",
        }
        baseline_columns = {c["name"]: c for c in inspector.get_columns("project_baselines")}
        assert baseline_columns["gate_decision_id"]["nullable"] is True
        assert baseline_columns["change_request_id"]["nullable"] is True
        assert "ck_project_baselines_made_by" in {
            ck["name"] for ck in inspector.get_check_constraints("project_baselines")
        }
        # 0021: the orchestrator's actions, inbox items and lease, and its audit trigger.
        orchestrator_tables = {"orchestrator_actions", "inbox_items", "orchestrator_leases"}
        assert orchestrator_tables <= set(inspector.get_table_names())
        for table in ("orchestrator_actions", "inbox_items"):
            assert {
                uq["name"]: uq["column_names"] for uq in inspector.get_unique_constraints(table)
            } == {f"uq_{table}_open_key": ["open_key"]}
        trigger_check = {
            ck["name"]: ck["sqltext"] for ck in inspector.get_check_constraints("pmo_audit")
        }["ck_pmo_audit_trigger"]
        assert "'orchestrator'" in trigger_check
        # 0022: who wrote each plan item.
        plan_item_tables = ("plan_milestones", "plan_deliverables", "plan_estimate_lines")
        for table in plan_item_tables:
            assert not {c["name"]: c for c in inspector.get_columns(table)}["source"]["nullable"]
            assert f"ck_{table}_source" in {
                ck["name"] for ck in inspector.get_check_constraints(table)
            }
        # 0023: the risk agent's suggested score, apart from the real one.
        suggested = (
            "suggested_probability",
            "suggested_impact",
            "suggested_probability_reason",
            "suggested_impact_reason",
            "suggested_model",
            "suggested_audit_id",
        )
        risk_columns = {c["name"]: c for c in inspector.get_columns("risks")}
        assert all(risk_columns[name]["nullable"] for name in suggested)
        # 0024: the simulated client, an event's source, and proposals with no person.
        assert not {c["name"]: c for c in inspector.get_columns("clients")}["simulated"]["nullable"]
        event_columns = {c["name"]: c for c in inspector.get_columns("proposal_events")}
        assert event_columns["actor_user_id"]["nullable"]
        assert not event_columns["source"]["nullable"]
        assert {c["name"]: c for c in inspector.get_columns("project_proposals")}["created_by"][
            "nullable"
        ]
        # 0028: status reports.
        assert {
            fk["name"]: fk["referred_table"] for fk in inspector.get_foreign_keys("status_reports")
        } == {
            "fk_status_reports_project": "projects",
            "fk_status_reports_replaces": "status_reports",
            "fk_status_reports_created_by": "users",
            "fk_status_reports_published_by": "users",
            "fk_status_reports_withdrawn_by": "users",
        }
        assert {ck["name"] for ck in inspector.get_check_constraints("status_reports")} == {
            "ck_status_reports_status",
            "ck_status_reports_period",
        }
        # 0029: who wrote each section of a status report, and reports with no person.
        report_columns = {c["name"]: c for c in inspector.get_columns("status_reports")}
        assert not report_columns["section_sources"]["nullable"]
        assert report_columns["created_by"]["nullable"]
        # 0030: read receipts, one row per report and reader.
        assert {
            fk["name"]: fk["referred_table"]
            for fk in inspector.get_foreign_keys("status_report_reads")
        } == {
            "fk_status_report_reads_report": "status_reports",
            "fk_status_report_reads_user": "users",
        }
        assert {
            uq["name"]: uq["column_names"]
            for uq in inspector.get_unique_constraints("status_report_reads")
        } == {"uq_status_report_reads_report_user": ["report_id", "user_id"]}
        # Fails if the models and the migrations disagree.
        command.check(config)
        command.downgrade(config, "0029")
        inspector = inspect(create_engine(get_settings().database_url))
        assert "status_report_reads" not in inspector.get_table_names()
        assert "status_reports" in inspector.get_table_names()
        command.downgrade(config, "0028")
        inspector = inspect(create_engine(get_settings().database_url))
        report_columns = {c["name"]: c for c in inspector.get_columns("status_reports")}
        assert "section_sources" not in report_columns
        assert not report_columns["created_by"]["nullable"]
        command.downgrade(config, "0027")
        inspector = inspect(create_engine(get_settings().database_url))
        assert "status_reports" not in inspector.get_table_names()
        assert "project_spend" in inspector.get_table_names()
        command.downgrade(config, "0023")
        inspector = inspect(create_engine(get_settings().database_url))
        assert "simulated" not in {c["name"] for c in inspector.get_columns("clients")}
        event_columns = {c["name"]: c for c in inspector.get_columns("proposal_events")}
        assert "source" not in event_columns
        assert event_columns["actor_user_id"]["nullable"] is False
        command.downgrade(config, "0022")
        inspector = inspect(create_engine(get_settings().database_url))
        assert not set(suggested) & {c["name"] for c in inspector.get_columns("risks")}
        command.downgrade(config, "0021")
        inspector = inspect(create_engine(get_settings().database_url))
        for table in plan_item_tables:
            assert "source" not in {c["name"] for c in inspector.get_columns(table)}
        command.downgrade(config, "0020")
        inspector = inspect(create_engine(get_settings().database_url))
        assert not orchestrator_tables & set(inspector.get_table_names())
        trigger_check = {
            ck["name"]: ck["sqltext"] for ck in inspector.get_check_constraints("pmo_audit")
        }["ck_pmo_audit_trigger"]
        assert "'orchestrator'" not in trigger_check and "'trial'" in trigger_check
        command.downgrade(config, "0019")
        inspector = inspect(create_engine(get_settings().database_url))
        assert "change_requests" not in inspector.get_table_names()
        baseline_columns = {c["name"]: c for c in inspector.get_columns("project_baselines")}
        assert "change_request_id" not in baseline_columns
        assert baseline_columns["gate_decision_id"]["nullable"] is False
        command.downgrade(config, "0018")
        inspector = inspect(create_engine(get_settings().database_url))
        assert "project_baselines" not in inspector.get_table_names()
        assert "version" not in {c["name"] for c in inspector.get_columns("projects")}
        assert "ck_projects_status" not in {
            ck["name"] for ck in inspector.get_check_constraints("projects")
        }
        command.downgrade(config, "0017")
        inspector = inspect(create_engine(get_settings().database_url))
        assert "section_sources" not in {
            c["name"] for c in inspector.get_columns("project_proposals")
        }
        with create_engine(get_settings().database_url).connect() as conn:
            proposals_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = 'project_proposals'"
            ).scalar_one()
            assert "AUTOINCREMENT" in proposals_sql
        command.downgrade(config, "0016")
        inspector = inspect(create_engine(get_settings().database_url))
        assert not set(inspector.get_table_names()) & {
            "project_proposals",
            "proposal_versions",
            "proposal_events",
        }
        assert "show_in_proposal" not in {c["name"] for c in inspector.get_columns("risks")}
        with create_engine(get_settings().database_url).connect() as conn:
            risks_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = 'risks'"
            ).scalar_one()
            assert "AUTOINCREMENT" in risks_sql
        command.downgrade(config, "0015")
        tables = set(inspect(create_engine(get_settings().database_url)).get_table_names())
        assert not tables & {
            "project_plans",
            "plan_milestones",
            "plan_deliverables",
            "plan_estimate_lines",
        }
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


def test_0018_marks_existing_sections_as_a_persons(tmp_path, monkeypatch):
    """Nothing written before the proposal agent counts as an agent's draft."""
    import json

    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'migrate.db'}")
    get_settings.cache_clear()
    try:
        config = Config(str(API_DIR / "alembic.ini"))
        command.upgrade(config, "0017")
        engine = create_engine(get_settings().database_url)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, password_hash, role, is_active, "
                "failed_logins, created_at) VALUES (1, 'pm@pmo.example', 'P', 'x', "
                "'project_manager', 1, 0, '2026-11-05')"
            )
            conn.exec_driver_sql(
                "INSERT INTO projects (id, name, stage, sponsor, pm, status, created_at) "
                "VALUES (1, 'F', 'planning', 'S', 'P', 'on_track', '2026-11-05')"
            )
            conn.exec_driver_sql(
                "INSERT INTO project_proposals (id, project_id, status, summary, scope_in, "
                "terms, price, estimate_lines, milestones, deliverables, risks, "
                "plan_version_seen, created_by, version, created_at, updated_at) VALUES "
                "(1, 1, 'draft', 'Our offer', 'Two sites', '  ', 10, '[]', '[]', '[]', '[]', 1, "
                "1, 1, '2026-11-05', '2026-11-05')"
            )
        command.upgrade(config, "0018")
        with engine.connect() as conn:
            stored = conn.exec_driver_sql(
                "SELECT section_sources FROM project_proposals WHERE id = 1"
            ).scalar_one()
        person = {"source": "person", "reviewed_by": None, "reviewed_at": None}
        assert json.loads(stored) == {"summary": person, "scope_in": person}
    finally:
        get_settings.cache_clear()


def _insert_0024_world(conn, simulated_answer: bool) -> None:
    conn.exec_driver_sql(
        "INSERT INTO users (id, email, display_name, password_hash, role, is_active, "
        "failed_logins, created_at) VALUES (1, 'pm@pmo.example', 'P', 'x', "
        "'project_manager', 1, 0, '2026-11-09')"
    )
    conn.exec_driver_sql(
        "INSERT INTO projects (id, name, stage, sponsor, pm, status, version, created_at) "
        "VALUES (1, 'F', 'planning', 'S', 'P', 'on_track', 1, '2026-11-09')"
    )
    conn.exec_driver_sql(
        "INSERT INTO project_proposals (id, project_id, status, price, estimate_lines, "
        "milestones, deliverables, risks, plan_version_seen, section_sources, created_by, "
        "version, created_at, updated_at) VALUES (1, 1, 'sent', 10, '[]', '[]', '[]', '[]', 1, "
        "'{}', 1, 1, '2026-11-09', '2026-11-09')"
    )
    conn.exec_driver_sql(
        "INSERT INTO proposal_events (id, proposal_id, kind, actor_user_id, created_at) "
        "VALUES (1, 1, 'sent', 1, '2026-11-09')"
    )
    if simulated_answer:
        conn.exec_driver_sql(
            "INSERT INTO proposal_events (id, proposal_id, kind, actor_user_id, source, answer, "
            "created_at) VALUES (2, 1, 'answer', NULL, 'simulated', 'accepted', '2026-11-09')"
        )


def test_0024_backfills_simulated_and_the_downgrade_deletes_simulated_answers(
    tmp_path, monkeypatch
):
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'migrate.db'}")
    get_settings.cache_clear()
    try:
        config = Config(str(API_DIR / "alembic.ini"))
        command.upgrade(config, "0023")
        engine = create_engine(get_settings().database_url)
        with engine.begin() as conn:
            for number, email in enumerate(
                ("buyer@quill.example", "Buyer@Quill.EXAMPLE ", "buyer@real-company.test", None),
                start=1,
            ):
                conn.exec_driver_sql(
                    "INSERT INTO clients (id, name, contact_email, created_at) "
                    "VALUES (?, 'C', ?, '2026-11-09')",
                    (number, email),
                )
        command.upgrade(config, "0024")
        with engine.begin() as conn:
            flags = dict(conn.exec_driver_sql("SELECT id, simulated FROM clients").all())
            assert flags == {1: 1, 2: 1, 3: 0, 4: 0}
            _insert_0024_world(conn, simulated_answer=True)
        # A person's event always has a person.
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO proposal_events (id, proposal_id, kind, actor_user_id, source, "
                "created_at) VALUES (3, 1, 'withdraw', NULL, 'person', '2026-11-09')"
            )
        command.downgrade(config, "0023")
        with engine.connect() as conn:
            kinds = conn.exec_driver_sql("SELECT id, kind FROM proposal_events").all()
        assert kinds == [(1, "sent")]
    finally:
        get_settings.cache_clear()


def test_0024_downgrade_refuses_while_the_orchestrator_made_a_proposal(tmp_path, monkeypatch):
    import pytest

    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'migrate.db'}")
    get_settings.cache_clear()
    try:
        config = Config(str(API_DIR / "alembic.ini"))
        command.upgrade(config, "0024")
        engine = create_engine(get_settings().database_url)
        with engine.begin() as conn:
            _insert_0024_world(conn, simulated_answer=False)
            conn.exec_driver_sql("UPDATE project_proposals SET created_by = NULL")
        with pytest.raises(RuntimeError, match="made by the orchestrator"):
            command.downgrade(config, "0023")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT created_by FROM project_proposals").scalar() is None
    finally:
        get_settings.cache_clear()


def test_0026_gives_existing_copies_their_submitter_and_drops_the_column_on_downgrade(
    tmp_path, monkeypatch
):
    import json

    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'migrate.db'}")
    get_settings.cache_clear()
    try:
        config = Config(str(API_DIR / "alembic.ini"))
        command.upgrade(config, "0025")
        engine = create_engine(get_settings().database_url)
        with engine.begin() as conn:
            _insert_0024_world(conn, simulated_answer=False)
            for number, submitter in ((1, 1), (2, None)):
                conn.exec_driver_sql(
                    "INSERT INTO proposal_versions (id, proposal_id, number, content, "
                    "submitted_by, submitted_at) VALUES (?, 1, ?, '{}', ?, '2026-11-09')",
                    (number, number, submitter),
                )
        command.upgrade(config, "0026")
        with engine.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT id, contributors FROM proposal_versions ORDER BY id"
            ).all()
        assert [(i, json.loads(c)) for i, c in rows] == [(1, [1]), (2, [])]
        columns = {c["name"]: c for c in inspect(engine).get_columns("proposal_versions")}
        assert columns["contributors"]["nullable"] is False
        command.downgrade(config, "0025")
        columns = {c["name"] for c in inspect(engine).get_columns("proposal_versions")}
        assert "contributors" not in columns
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM proposal_versions").scalar() == 2
    finally:
        get_settings.cache_clear()


def test_0029_marks_existing_report_sections_as_a_persons_and_guards_the_downgrade(
    tmp_path, monkeypatch
):
    """Nothing written before the status report agent counts as its draft; a report the
    orchestrator made (no person) stops the downgrade."""
    import json

    import pytest

    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'migrate.db'}")
    get_settings.cache_clear()
    try:
        config = Config(str(API_DIR / "alembic.ini"))
        command.upgrade(config, "0028")
        engine = create_engine(get_settings().database_url)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, password_hash, role, is_active, "
                "failed_logins, created_at) VALUES (1, 'pm@pmo.example', 'P', 'x', "
                "'project_manager', 1, 0, '2026-11-12')"
            )
            conn.exec_driver_sql(
                "INSERT INTO projects (id, name, stage, sponsor, pm, status, version, created_at) "
                "VALUES (1, 'F', 'delivery', 'S', 'P', 'on_track', 1, '2026-11-12')"
            )
            conn.exec_driver_sql(
                "INSERT INTO status_reports (id, project_id, period_start, period_end, due_on, "
                "status, figures, ratings, sections, flags, figures_at, created_by, version, "
                "created_at, updated_at) VALUES (1, 1, '2027-03-01', '2027-03-07', "
                "'2027-03-08', 'draft', '[]', '{}', "
                '\'{"summary": "On track", "done": "  ", "next": null}\', '
                "'[]', '2027-03-08', 1, 1, '2027-03-08', '2027-03-08')"
            )
        command.upgrade(config, "0029")
        with engine.connect() as conn:
            stored = conn.exec_driver_sql(
                "SELECT section_sources FROM status_reports WHERE id = 1"
            ).scalar_one()
        person = {"source": "person", "reviewed_by": None, "reviewed_at": None}
        assert json.loads(stored) == {"summary": person}
        with engine.begin() as conn:
            conn.exec_driver_sql("UPDATE status_reports SET created_by = NULL")
        with pytest.raises(RuntimeError, match="made by the orchestrator"):
            command.downgrade(config, "0028")
        with engine.begin() as conn:
            conn.exec_driver_sql("UPDATE status_reports SET created_by = 1")
        command.downgrade(config, "0028")
    finally:
        get_settings.cache_clear()
