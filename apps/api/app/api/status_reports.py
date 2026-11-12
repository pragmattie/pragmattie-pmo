"""Status reports (item 7.3): each project in delivery gets a report each period, whose figures
come from records and whose ratings come from written rules (`app.reports`).

- `GET /projects/{id}/status-reports` (`project:read`): the project's periods, newest first and
  paged, each with the reports the caller may see.
- `POST /projects/{id}/status-reports` (`report:publish`): a draft for the period ending on
  `period_end`, with its figures, ratings and empty text. A period has at most one draft and one
  published report. A correction of a published report names it (`replaces_id`) with a reason.
- `GET .../{rid}` (`project:read`): one report.
- `PATCH .../{rid}` (`report:publish`): edit a draft's text sections.
- `POST .../{rid}/refresh` (`report:publish`): read a draft's figures from the records again and
  rate them again.
- `POST .../{rid}/publish` (`report:publish`): publish a draft. Refused (409, with the flags)
  while the checker flags anything in the text. A published report is frozen; publishing a
  correction withdraws the report it replaces, with the correction's reason.
- `POST .../{rid}/withdraw` (`report:publish`): withdraw a draft or a published report, with a
  reason.

Only roles with `report:publish` see drafts (and drafts withdrawn before they were published).
Anyone else gets a 404 for one, so a draft's existence isn't shown; they see published reports
and withdrawn ones that had been published.

Creating a draft and refreshing one need a project in delivery that isn't cancelled, a reporting
period in the plan, and a period of the project's. Every write on a report carries its version
(stale is a 409). Every write is one audit row (agent `status_report`), and so is every refused
attempt, a role without `report:publish` included. Nothing is sent anywhere from here. The
ratings come from the reporting policy file (`app.policy.get_reporting`); until a person applies
it, or while it is invalid, creating and refreshing answer 503, recorded as a refusal
(`policy_unavailable`).
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import delivery
from app import policy as policies
from app import reports as rules
from app.api.intake import _commit, _names
from app.api.projects import Db, Reader
from app.audit import next_attempt, record_decision, record_refusal
from app.authz import Action, can, require_audited
from app.models import Project, ProjectPlan, User
from app.models.base import utcnow
from app.models.report import DRAFT, PUBLISHED, SECTIONS, WITHDRAWN, StatusReport
from app.policy import PolicyError, ReportingPolicy
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset
from app.schemas.report import (
    PeriodOut,
    PeriodsPage,
    ReportCreate,
    ReportOut,
    ReportSummaryOut,
    ReportTextIn,
    ReportVersionIn,
    ReportWithdrawIn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["status reports"])

AUDIT_AGENT = "status_report"
SUBJECT = "status_report"
PROJECT_SUBJECT = "project"

PROJECT_NOT_FOUND = "project not found"
REPORT_NOT_FOUND = "status report not found"
UNAVAILABLE = (
    "status reports aren't available yet: a person needs to apply the reporting policy file"
)
STALE = "this has changed since you read it; reload it and try again"
NO_PERIOD = "the plan has no reporting period, so a report has no period to cover"
NO_BASELINE = "the project has no baseline yet, so there is nothing to report against"
UNKNOWN_PERIOD = "no reporting period of this project ends on that day"
DRAFT_EXISTS = "this period already has a draft; edit it instead"
ALREADY_PUBLISHED = (
    "this period's report is published; a correction must name it in replaces_id, with a reason"
)
BAD_REPLACES = "a correction must name this period's published report"
NEEDS_REASON = "a correction needs the reason the report it replaces is withdrawn"
NOT_A_CORRECTION = "only a correction names a report it replaces"
NOT_DRAFT = "only a draft can change; a published report is frozen"
HAS_FLAGS = "the report can't be published while the checker has flags open"
ALREADY_WITHDRAWN = "this report is already withdrawn"


# --- Loading


def _project(db: Session, project_id: int, *, for_update: bool = False) -> Project:
    stmt = select(Project).where(Project.id == project_id)
    project = db.scalar(stmt.with_for_update() if for_update else stmt)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, PROJECT_NOT_FOUND)
    return project


def sees_drafts(user: User) -> bool:
    return can(user.role, Action.REPORT_PUBLISH)


def _visible(report: StatusReport, user: User) -> bool:
    return sees_drafts(user) or report.published_at is not None


def _report(
    db: Session, project_id: int, report_id: int, user: User, *, lock: bool = False
) -> StatusReport:
    stmt = select(StatusReport).where(
        StatusReport.id == report_id, StatusReport.project_id == project_id
    )
    report = db.scalar(stmt.with_for_update() if lock else stmt)
    if report is None or not _visible(report, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, REPORT_NOT_FOUND)
    return report


def _plan(db: Session, project_id: int) -> ProjectPlan | None:
    return db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project_id))


def _policy(
    db: Session, user: User, project: Project, report: StatusReport | None, action: str
) -> ReportingPolicy:
    """The reporting policy, or the refusal (503, one audit row) while it's missing or invalid."""
    try:
        return policies.get_reporting()
    except PolicyError:
        logger.exception("reporting policy failed to load")
    raise _refuse(
        db,
        user,
        project,
        report,
        action=action,
        refusal="policy_unavailable",
        code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=UNAVAILABLE,
    )


def _periods(db: Session, project: Project, plan: ProjectPlan | None) -> list[rules.Period]:
    """The project's periods, oldest first: none without a reporting period or baseline 1."""
    if plan is None or plan.report_period is None or plan.report_due_day is None:
        return []
    first = rules.first_baseline(db, project.id)
    if first is None:
        return []
    return rules.periods(
        plan.report_period, plan.report_due_day, first.created_at.date(), delivery.today()
    )


# --- Audit


def _snapshot(report: StatusReport) -> dict[str, Any]:
    return {
        "status": report.status,
        "period_start": report.period_start.isoformat(),
        "period_end": report.period_end.isoformat(),
        "figures": report.figures,
        "ratings": {name: r["rating"] for name, r in report.ratings.items()},
        "sections": report.sections,
        "flags": report.flags,
        "replaces_id": report.replaces_id,
        "replace_reason": report.replace_reason,
        "withdrawn_reason": report.withdrawn_reason,
    }


def _refuse(
    db: Session,
    user: User,
    project: Project,
    report: StatusReport | None,
    *,
    action: str,
    refusal: str,
    code: int,
    detail: Any,
    **extra: Any,
) -> HTTPException:
    """Record and commit a refused attempt on the report (or the project when there is none),
    and give the error to raise."""
    if report is not None:
        subject_type, subject_id, version = SUBJECT, report.id, report.version
        output = {"status": report.status, "period_end": report.period_end.isoformat()}
    else:
        subject_type, subject_id, version = PROJECT_SUBJECT, project.id, project.version
        output = {"stage": project.stage, "status": project.status}
    record_refusal(
        db,
        agent=AUDIT_AGENT,
        subject_type=subject_type,
        subject_id=str(subject_id),
        version=version,
        actor_user_id=user.id,
        output=output,
        action_taken={
            "action": f"{action}_refused",
            "project_id": project.id,
            "refusal": refusal,
            "actor_role": user.role,
            **extra,
        },
    )
    _commit(db)
    return HTTPException(code, detail)


def _record(
    db: Session,
    user: User,
    report: StatusReport,
    *,
    before: dict[str, Any] | None,
    action: str,
    **extra: Any,
) -> None:
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=SUBJECT,
        subject_id=str(report.id),
        version=report.version,
        attempt=next_attempt(
            db,
            agent=AUDIT_AGENT,
            subject_type=SUBJECT,
            subject_id=str(report.id),
            version=report.version,
        ),
        status="ok",
        actor_user_id=user.id,
        signals=before,
        output=_snapshot(report),
        action_taken={"action": action, "project_id": report.project_id, **extra},
    )


def _refusal_recorder(action: str):
    """The guard's refusal row for a role without `report:publish`. An unknown project or a
    report the caller can't see has nothing to name, so it gets the 403 with the project as
    subject, or without a row when the project is unknown."""

    def record(db: Session, user: User, path_params: dict[str, str]) -> None:
        try:
            project = _project(db, int(path_params["project_id"]))
        except (KeyError, ValueError, HTTPException):
            return
        report = None
        if "report_id" in path_params:
            try:
                report = _report(db, project.id, int(path_params["report_id"]), user)
            except (ValueError, HTTPException):
                report = None
        subject_type, subject_id, version = (
            (SUBJECT, report.id, report.version)
            if report
            else (PROJECT_SUBJECT, project.id, project.version)
        )
        record_refusal(
            db,
            agent=AUDIT_AGENT,
            subject_type=subject_type,
            subject_id=str(subject_id),
            version=version,
            actor_user_id=user.id,
            output={"stage": project.stage, "status": project.status}
            if report is None
            else {"status": report.status, "period_end": report.period_end.isoformat()},
            action_taken={
                "action": f"{action}_refused",
                "project_id": project.id,
                "refusal": "no_permission",
                "actor_role": user.role,
            },
        )

    return record


def _writer(action: str):
    return Annotated[
        User, Depends(require_audited(Action.REPORT_PUBLISH, _refusal_recorder(action)))
    ]


CreateWriter = _writer("create")
TextWriter = _writer("edit_text")
RefreshWriter = _writer("refresh")
PublishWriter = _writer("publish")
WithdrawWriter = _writer("withdraw")


def _locked_project(db: Session, project_id: int) -> Project:
    """The project, locked until commit on MySQL so writes on its reports take turns. The guard's
    read-only transaction ends first, so a write that waited sees what the one before it
    committed."""
    db.rollback()
    return _project(db, project_id, for_update=True)


def _locked_report(
    db: Session, user: User, project: Project, report_id: int, version: int, action: str
) -> StatusReport:
    report = _report(db, project.id, report_id, user, lock=True)
    if report.version != version:
        raise _refuse(
            db,
            user,
            project,
            report,
            action=action,
            refusal="stale_version",
            code=status.HTTP_409_CONFLICT,
            detail=STALE,
        )
    return report


def _require_draft(
    db: Session, user: User, project: Project, report: StatusReport, action: str
) -> None:
    if report.status != DRAFT:
        raise _refuse(
            db,
            user,
            project,
            report,
            action=action,
            refusal="not_draft",
            code=status.HTTP_409_CONFLICT,
            detail=NOT_DRAFT,
        )


def _reportable(
    db: Session, user: User, project: Project, report: StatusReport | None, action: str
) -> tuple[ProjectPlan, list[rules.Period]]:
    """The plan and periods of a project figures can be read for, or the refusal."""
    refused = delivery.refusal(project)
    plan = _plan(db, project.id)
    if refused is None and (plan is None or plan.report_period is None):
        refused = ("no_report_period", NO_PERIOD)
    if refused is None and rules.first_baseline(db, project.id) is None:
        refused = ("no_baseline", NO_BASELINE)
    if refused is not None:
        raise _refuse(
            db,
            user,
            project,
            report,
            action=action,
            refusal=refused[0],
            code=status.HTTP_409_CONFLICT,
            detail=refused[1],
        )
    assert plan is not None
    return plan, _periods(db, project, plan)


def _fill(
    db: Session,
    report: StatusReport,
    plan: ProjectPlan,
    period: rules.Period,
    policy: ReportingPolicy,
) -> None:
    """Read the figures from the records, rate them and run the checker."""
    next_ = rules.next_period(plan.report_period, plan.report_due_day, period)
    report.figures_at = utcnow()
    report.figures = rules.build_figures(
        db, report.project_id, plan, period, next_, report.figures_at.date()
    )
    report.ratings = rules.rate(report.figures, policy)
    _check(report)


def _period_of(report: StatusReport) -> rules.Period:
    return rules.Period(report.period_start, report.period_end, report.due_on)


def _check(report: StatusReport) -> list[dict[str, str]]:
    report.flags = [
        f.as_dict() for f in rules.check(report.sections, report.figures, _period_of(report))
    ]
    return report.flags


# --- Output


def _out(db: Session, report: StatusReport) -> ReportOut:
    people = _names(db, User, {report.created_by, report.published_by} - {None}, User.display_name)
    replaced_by = db.scalar(
        select(StatusReport.id)
        .where(StatusReport.replaces_id == report.id, StatusReport.status == PUBLISHED)
        .limit(1)
    )
    return ReportOut.model_validate(
        {
            **{c: getattr(report, c) for c in StatusReport.__table__.columns.keys()},
            "replaced_by_id": replaced_by,
            "created_by_name": people.get(report.created_by),
            "published_by_name": people.get(report.published_by),
        }
    )


def _summary(report: StatusReport) -> ReportSummaryOut:
    return ReportSummaryOut(
        id=report.id,
        status=report.status,
        overall=report.ratings["overall"]["rating"],
        replaces_id=report.replaces_id,
        published_at=report.published_at,
        withdrawn_at=report.withdrawn_at,
        version=report.version,
    )


# --- Routes


@router.get("/{project_id}/status-reports", response_model=PeriodsPage)
def list_status_reports(
    project_id: int,
    user: Reader,
    db: Db,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> PeriodsPage:
    project = _project(db, project_id)
    plan = _plan(db, project.id)
    newest_first = list(reversed(_periods(db, project, plan)))
    page = newest_first[offset : offset + limit]
    reports = [
        r
        for r in db.scalars(
            select(StatusReport)
            .where(StatusReport.project_id == project.id)
            .order_by(StatusReport.id.desc())
        )
        if _visible(r, user)
    ]
    return PeriodsPage(
        project_id=project.id,
        report_period=plan.report_period if plan else None,
        report_due_day=plan.report_due_day if plan else None,
        items=[
            PeriodOut(
                start=p.start,
                end=p.end,
                due_on=p.due_on,
                reports=[_summary(r) for r in reports if r.period_end == p.end],
            )
            for p in page
        ],
        total=len(newest_first),
        limit=limit,
        offset=offset,
    )


@router.post("/{project_id}/status-reports", response_model=ReportOut, status_code=201)
def create_status_report(
    project_id: int, body: ReportCreate, user: CreateWriter, db: Db
) -> ReportOut:
    action = "create"
    project = _locked_project(db, project_id)
    policy = _policy(db, user, project, None, action)
    plan, periods = _reportable(db, user, project, None, action)
    period = next((p for p in periods if p.end == body.period_end), None)
    if period is None:
        raise _refuse(
            db,
            user,
            project,
            None,
            action=action,
            refusal="unknown_period",
            code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=UNKNOWN_PERIOD,
            period_end=body.period_end.isoformat(),
        )
    live = {
        r.status: r
        for r in db.scalars(
            select(StatusReport).where(
                StatusReport.project_id == project.id,
                StatusReport.period_end == period.end,
                StatusReport.status != WITHDRAWN,
            )
        )
    }
    published = live.get(PUBLISHED)
    refusal: tuple[str, int, str] | None = None
    if DRAFT in live:
        refusal = ("draft_exists", status.HTTP_409_CONFLICT, DRAFT_EXISTS)
    elif published is not None and body.replaces_id is None:
        refusal = ("already_published", status.HTTP_409_CONFLICT, ALREADY_PUBLISHED)
    elif body.replaces_id is not None and (published is None or published.id != body.replaces_id):
        refusal = ("bad_replaces", status.HTTP_409_CONFLICT, BAD_REPLACES)
    elif body.replaces_id is not None and body.reason is None:
        refusal = ("needs_reason", status.HTTP_422_UNPROCESSABLE_CONTENT, NEEDS_REASON)
    elif body.replaces_id is None and body.reason is not None:
        refusal = ("not_a_correction", status.HTTP_422_UNPROCESSABLE_CONTENT, NOT_A_CORRECTION)
    if refusal is not None:
        raise _refuse(
            db,
            user,
            project,
            None,
            action=action,
            refusal=refusal[0],
            code=refusal[1],
            detail=refusal[2],
            period_end=period.end.isoformat(),
            replaces_id=body.replaces_id,
        )
    report = StatusReport(
        project_id=project.id,
        period_start=period.start,
        period_end=period.end,
        due_on=period.due_on,
        status=DRAFT,
        sections=dict.fromkeys(SECTIONS),
        replaces_id=body.replaces_id,
        replace_reason=body.reason,
        created_by=user.id,
    )
    _fill(db, report, plan, period, policy)
    db.add(report)
    db.flush()
    _record(
        db,
        user,
        report,
        before=None,
        action="correction_created" if body.replaces_id else "created",
    )
    _commit(db)
    return _out(db, report)


@router.get("/{project_id}/status-reports/{report_id}", response_model=ReportOut)
def get_status_report(project_id: int, report_id: int, user: Reader, db: Db) -> ReportOut:
    project = _project(db, project_id)
    return _out(db, _report(db, project.id, report_id, user))


@router.patch("/{project_id}/status-reports/{report_id}", response_model=ReportOut)
def edit_status_report(
    project_id: int, report_id: int, body: ReportTextIn, user: TextWriter, db: Db
) -> ReportOut:
    action = "edit_text"
    project = _locked_project(db, project_id)
    report = _locked_report(db, user, project, report_id, body.version, action)
    _require_draft(db, user, project, report, action)
    before = _snapshot(report)
    changed = {k: (v or None) for k, v in body.sections.model_dump(exclude_unset=True).items()}
    report.sections = {**report.sections, **changed}
    _check(report)
    db.flush()
    _record(db, user, report, before=before, action="text_edited", sections=sorted(changed))
    _commit(db)
    return _out(db, report)


@router.post("/{project_id}/status-reports/{report_id}/refresh", response_model=ReportOut)
def refresh_status_report(
    project_id: int, report_id: int, body: ReportVersionIn, user: RefreshWriter, db: Db
) -> ReportOut:
    action = "refresh"
    project = _locked_project(db, project_id)
    report = _locked_report(db, user, project, report_id, body.version, action)
    _require_draft(db, user, project, report, action)
    policy = _policy(db, user, project, report, action)
    plan, _ = _reportable(db, user, project, report, action)
    before = _snapshot(report)
    _fill(db, report, plan, _period_of(report), policy)
    db.flush()
    _record(db, user, report, before=before, action="refreshed")
    _commit(db)
    return _out(db, report)


@router.post("/{project_id}/status-reports/{report_id}/publish", response_model=ReportOut)
def publish_status_report(
    project_id: int, report_id: int, body: ReportVersionIn, user: PublishWriter, db: Db
) -> ReportOut:
    action = "publish"
    project = _locked_project(db, project_id)
    report = _locked_report(db, user, project, report_id, body.version, action)
    _require_draft(db, user, project, report, action)
    flags = rules.check(report.sections, report.figures, _period_of(report))
    if flags:
        found = [f.as_dict() for f in flags]
        raise _refuse(
            db,
            user,
            project,
            report,
            action=action,
            refusal="flags_open",
            code=status.HTTP_409_CONFLICT,
            detail={"message": HAS_FLAGS, "flags": found},
            flags=found,
        )
    before = _snapshot(report)
    now = utcnow()
    withdrawn_id = None
    if report.replaces_id is not None:
        old = db.scalar(
            select(StatusReport).where(StatusReport.id == report.replaces_id).with_for_update()
        )
        if old is not None and old.status == PUBLISHED:
            old.status = WITHDRAWN
            old.withdrawn_by = user.id
            old.withdrawn_at = now
            old.withdrawn_reason = report.replace_reason
            withdrawn_id = old.id
    report.status = PUBLISHED
    report.flags = []
    report.published_by = user.id
    report.published_at = now
    db.flush()
    _record(db, user, report, before=before, action="published", withdrew_id=withdrawn_id)
    _commit(db)
    return _out(db, report)


@router.post("/{project_id}/status-reports/{report_id}/withdraw", response_model=ReportOut)
def withdraw_status_report(
    project_id: int, report_id: int, body: ReportWithdrawIn, user: WithdrawWriter, db: Db
) -> ReportOut:
    action = "withdraw"
    project = _locked_project(db, project_id)
    report = _locked_report(db, user, project, report_id, body.version, action)
    if report.status == WITHDRAWN:
        raise _refuse(
            db,
            user,
            project,
            report,
            action=action,
            refusal="already_withdrawn",
            code=status.HTTP_409_CONFLICT,
            detail=ALREADY_WITHDRAWN,
        )
    before = _snapshot(report)
    report.status = WITHDRAWN
    report.withdrawn_by = user.id
    report.withdrawn_at = utcnow()
    report.withdrawn_reason = body.reason
    db.flush()
    _record(db, user, report, before=before, action="withdrawn")
    _commit(db)
    return _out(db, report)
