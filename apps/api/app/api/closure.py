"""Closing a project (item 7.7): its closure report, each benefit as measured at close, and its
lessons. Figures come from the records (`app.closure`), as a status report's do (item 7.3).

- `GET /projects/{id}/closure` (`project:read`): the closure report (null until one is made), each
  value case benefit as measured at close, the lessons (discarded ones included) and the
  accepted risks still standing.
- `POST /projects/{id}/closure-report` (`report:publish`): make the report, with its figures and
  empty text. One per project.
- `PATCH .../closure-report` (`report:publish`): edit the text; each section whose text changes
  becomes the person's.
- `POST .../closure-report/refresh` (`report:publish`): read the figures from the records again.
- `POST .../closure-report/sections/{section}/confirm` (`report:publish`): a person accepts an
  agent's draft section as it is.
- `POST .../closure-report/draft` (`project:write`): run the closure agent
  (`app.agents.closure_agent`), gated by `AGENT_MODE` as the status report agent is: `off`
  refuses (409, recorded), `shadow` saves nothing, `enforce` saves each section that is empty or
  still its unreviewed draft, and the lessons it suggests. `?dry_run=true` returns the exact
  request and its most possible cost, and calls nothing.
- `PUT .../closure-report/benefits/{benefit_id}` (`report:publish`): a person enters a benefit's
  value now and its date, or the owner (an active user) and date to measure it later. Only a
  person: the agent has no such field.
- `POST /projects/{id}/lessons` (`project:write`): record a lesson. `PATCH .../lessons/{lid}`
  edits one (an agent's suggestion edited is recorded by the person), `POST .../record` records a
  suggestion as it is, and `POST .../discard` discards a suggestion.

The report, its benefits and the agent work only for a project in `closure`; lessons are recorded
in `delivery` or `closure`. Anything else is refused (409), and once the project is `closed` every
write is refused: the close-out gate froze the record. Every write carries its version (stale is a
409), and every write and every refusal, a role without the permission included, is one audit
row (agent `closure`). Nothing is sent anywhere from here.
"""

import logging
from decimal import ROUND_UP, Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import closure as rules
from app import reports
from app.agents import closure_agent, risk_agent
from app.api.intake import _commit, _names
from app.api.projects import Db, Reader
from app.audit import record_refusal
from app.authz import Action, require_audited
from app.config import get_settings
from app.models import ClosureBenefit, ClosureReport, Lesson, Project, User
from app.models.base import utcnow
from app.models.closure import CLOSURE_SECTIONS, DISCARDED, RECORDED, SUGGESTED
from app.schemas.closure import (
    AcceptedRiskOut,
    BenefitIn,
    BenefitOut,
    ClosureDraftResult,
    ClosureOut,
    ClosureReportOut,
    ClosureTextIn,
    ClosureVersionIn,
    LessonEditIn,
    LessonIn,
    LessonOut,
    LessonVersionIn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["closure"])

AUDIT_AGENT = rules.AUDIT_AGENT
PROJECT_SUBJECT = "project"

PROJECT_NOT_FOUND = "project not found"
NO_REPORT = "this project has no closure report yet"
LESSON_NOT_FOUND = "lesson not found"
BENEFIT_NOT_FOUND = "this benefit isn't in the project's value case"
STALE = "this has changed since you read it; reload it and try again"
REPORT_EXISTS = "this project already has a closure report; edit it instead"
NO_BASELINE = "the project has no baseline, so there is nothing to report against"
NOT_AGENT_DRAFT = "only a section that is an agent's unreviewed draft can be confirmed"
AGENT_OFF = "the closure agent is off (AGENT_MODE=off); a person can write the report by hand"
AGENT_FAILED = "the closure agent couldn't draft the report; nothing was saved"
VALUE_NEEDS_DATE = "a value now needs the day it was measured, and a day needs a value"
OWNER_NEEDS_DATE = "an owner to measure the benefit needs a date, and a date needs an owner"
NOTHING_ENTERED = "enter a value now with its date, or an owner and a date to measure it"
BAD_OWNER = "the owner must be an active user"
NEEDS_TAKEAWAY = "a lesson needs what we would do again or what we would do differently"
NOT_SUGGESTED = "only a lesson the agent suggested and nobody has recorded can be {what}"
LESSON_DISCARDED = "this lesson was discarded"

ClosureSection = Literal["delivered", "value_case", "remaining"]


# --- Loading and refusing


def _project(db: Session, project_id: int, *, for_update: bool = False) -> Project:
    stmt = select(Project).where(Project.id == project_id)
    project = db.scalar(stmt.with_for_update() if for_update else stmt)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, PROJECT_NOT_FOUND)
    return project


def _locked_project(db: Session, project_id: int) -> Project:
    """The project, locked until commit on MySQL so writes on its closure take turns. The guard's
    read-only transaction ends first, so a write that waited sees what the one before it
    committed."""
    db.rollback()
    return _project(db, project_id, for_update=True)


def _refuse(
    db: Session,
    user: User,
    project: Project,
    *,
    action: str,
    refusal: str,
    code: int,
    detail: Any,
    subject: tuple[str, int, int] | None = None,
    **extra: Any,
) -> HTTPException:
    """Record and commit a refused attempt on `subject` (type, id, version), or the project when
    there is none, and give the error to raise."""
    subject_type, subject_id, version = subject or (PROJECT_SUBJECT, project.id, project.version)
    record_refusal(
        db,
        agent=AUDIT_AGENT,
        subject_type=subject_type,
        subject_id=str(subject_id),
        version=version,
        actor_user_id=user.id,
        output={"stage": project.stage, "status": project.status},
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


def _refusal_recorder(action: str):
    """The guard's refusal row for a role without the permission, on the project. An unknown
    project has nothing to name, so it gets the 403 without a row."""

    def record(db: Session, user: User, path_params: dict[str, str]) -> None:
        try:
            project = _project(db, int(path_params["project_id"]))
        except (KeyError, ValueError, HTTPException):
            return
        record_refusal(
            db,
            agent=AUDIT_AGENT,
            subject_type=PROJECT_SUBJECT,
            subject_id=str(project.id),
            version=project.version,
            actor_user_id=user.id,
            output={"stage": project.stage, "status": project.status},
            action_taken={
                "action": f"{action}_refused",
                "project_id": project.id,
                "refusal": "no_permission",
                "actor_role": user.role,
            },
        )

    return record


def _guard(action: Action, name: str):
    return Annotated[User, Depends(require_audited(action, _refusal_recorder(name)))]


Creator = _guard(Action.REPORT_PUBLISH, "create")
TextWriter = _guard(Action.REPORT_PUBLISH, "edit_text")
Refresher = _guard(Action.REPORT_PUBLISH, "refresh")
Confirmer = _guard(Action.REPORT_PUBLISH, "confirm")
BenefitWriter = _guard(Action.REPORT_PUBLISH, "measure_benefit")
Drafter = _guard(Action.PROJECT_WRITE, "draft")
LessonWriter = _guard(Action.PROJECT_WRITE, "record_lesson")
LessonEditor = _guard(Action.PROJECT_WRITE, "edit_lesson")
LessonRecorder = _guard(Action.PROJECT_WRITE, "confirm_lesson")
LessonDiscarder = _guard(Action.PROJECT_WRITE, "discard_lesson")


def _in_closure(
    db: Session,
    user: User,
    project: Project,
    action: str,
    subject: tuple[str, int, int] | None = None,
) -> None:
    refused = rules.refusal(project)
    if refused is not None:
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal=refused[0],
            code=status.HTTP_409_CONFLICT,
            detail=refused[1],
            subject=subject,
        )


def _report_subject(report: ClosureReport) -> tuple[str, int, int]:
    return (rules.SUBJECT, report.id, report.version)


def _locked_report(
    db: Session, user: User, project: Project, version: int, action: str
) -> ClosureReport:
    """The project's report, locked, in a project in closure, at `version`; or the refusal."""
    report = rules.report_of(db, project.id, lock=True)
    if report is None:
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal="no_report",
            code=status.HTTP_404_NOT_FOUND,
            detail=NO_REPORT,
        )
    _in_closure(db, user, project, action, _report_subject(report))
    if report.version != version:
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal="stale_version",
            code=status.HTTP_409_CONFLICT,
            detail=STALE,
            subject=_report_subject(report),
        )
    return report


# --- Output


def _section_states(report: ClosureReport) -> dict[str, dict[str, Any]]:
    sources = report.section_sources or {}
    return {
        name: {
            "source": (sources.get(name) or {}).get("source"),
            "reviewed_by": (sources.get(name) or {}).get("reviewed_by"),
            "reviewed_at": (sources.get(name) or {}).get("reviewed_at"),
            "unreviewed": reports.is_unreviewed(report, name),
        }
        for name in CLOSURE_SECTIONS
    }


def _report_out(report: ClosureReport) -> ClosureReportOut:
    return ClosureReportOut.model_validate(
        {
            **{c: getattr(report, c) for c in ClosureReport.__table__.columns.keys()},
            "section_states": _section_states(report),
        }
    )


def _out(db: Session, project: Project) -> ClosureOut:
    report = rules.report_of(db, project.id)
    benefits = rules.benefits_snapshot(db, project.id)
    owners = _names(db, User, {b["measure_owner_id"] for b in benefits} - {None}, User.display_name)
    lessons = db.scalars(select(Lesson).where(Lesson.project_id == project.id).order_by(Lesson.id))
    return ClosureOut(
        project_id=project.id,
        project_name=project.name,
        project_stage=project.stage,
        project_status=project.status,
        report=_report_out(report) if report is not None else None,
        benefits=[
            BenefitOut.model_validate(
                {**b, "measure_owner_name": owners.get(b["measure_owner_id"])}
            )
            for b in benefits
        ],
        lessons=[LessonOut.model_validate(lesson, from_attributes=True) for lesson in lessons],
        accepted_risks=[
            AcceptedRiskOut(id=r.id, title=r.title, band=r.band)
            for r in rules.accepted_risks(db, project.id)
        ],
    )


# --- The closure report


@router.get("/{project_id}/closure", response_model=ClosureOut)
def get_closure(project_id: int, _user: Reader, db: Db) -> ClosureOut:
    return _out(db, _project(db, project_id))


@router.post("/{project_id}/closure-report", response_model=ClosureOut, status_code=201)
def create_closure_report(project_id: int, user: Creator, db: Db) -> ClosureOut:
    action = "create"
    project = _locked_project(db, project_id)
    _in_closure(db, user, project, action)
    refusal = None
    if rules.report_of(db, project.id) is not None:
        refusal = ("report_exists", REPORT_EXISTS)
    elif reports.first_baseline(db, project.id) is None:
        refusal = ("no_baseline", NO_BASELINE)
    if refusal is not None:
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal=refusal[0],
            code=status.HTTP_409_CONFLICT,
            detail=refusal[1],
        )
    report = rules.new_report(db, project, created_by=user.id)
    rules.record_report(db, report, actor_user_id=user.id, before=None, action="created")
    _commit(db)
    return _out(db, project)


@router.patch("/{project_id}/closure-report", response_model=ClosureOut)
def edit_closure_report(
    project_id: int, body: ClosureTextIn, user: TextWriter, db: Db
) -> ClosureOut:
    action = "edit_text"
    project = _locked_project(db, project_id)
    report = _locked_report(db, user, project, body.version, action)
    before = rules.snapshot(report)
    changed = {k: (v or None) for k, v in body.sections.model_dump(exclude_unset=True).items()}
    # A section whose text the person changed is now theirs, so the agent never replaces it.
    edited = [n for n, text in changed.items() if (report.sections.get(n) or None) != text]
    report.sections = {**report.sections, **changed}
    now = utcnow().isoformat()
    for name in edited:
        reports.mark_section(
            report, name, source=reports.PERSON_SOURCE, reviewed_by=user.id, reviewed_at=now
        )
    rules.run_check(report)
    db.flush()
    rules.record_report(
        db,
        report,
        actor_user_id=user.id,
        before=before,
        action="text_edited",
        sections=sorted(changed),
    )
    _commit(db)
    return _out(db, project)


@router.post("/{project_id}/closure-report/refresh", response_model=ClosureOut)
def refresh_closure_report(
    project_id: int, body: ClosureVersionIn, user: Refresher, db: Db
) -> ClosureOut:
    action = "refresh"
    project = _locked_project(db, project_id)
    report = _locked_report(db, user, project, body.version, action)
    before = rules.snapshot(report)
    rules.fill(db, report, project)
    db.flush()
    rules.record_report(db, report, actor_user_id=user.id, before=before, action="refreshed")
    _commit(db)
    return _out(db, project)


@router.post("/{project_id}/closure-report/sections/{section}/confirm", response_model=ClosureOut)
def confirm_closure_section(
    project_id: int, section: ClosureSection, body: ClosureVersionIn, user: Confirmer, db: Db
) -> ClosureOut:
    action = "confirm"
    project = _locked_project(db, project_id)
    report = _locked_report(db, user, project, body.version, action)
    if not reports.is_unreviewed(report, section):
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal="not_agent_draft",
            code=status.HTTP_409_CONFLICT,
            detail=NOT_AGENT_DRAFT,
            subject=_report_subject(report),
            section=section,
        )
    before = rules.snapshot(report)
    reports.mark_section(
        report,
        section,
        source=reports.AGENT_SOURCE,
        reviewed_by=user.id,
        reviewed_at=utcnow().isoformat(),
    )
    db.flush()
    rules.record_report(
        db,
        report,
        actor_user_id=user.id,
        before=before,
        action="section_confirmed",
        section=section,
    )
    _commit(db)
    return _out(db, project)


# --- The closure agent


def get_claude_client() -> risk_agent.ClaudeClient:
    """The closure agent's Claude client; tests replace it through `dependency_overrides`."""
    return risk_agent.AnthropicClient(get_settings())


@router.post("/{project_id}/closure-report/draft", response_model=ClosureDraftResult)
def draft_closure_report(
    project_id: int,
    user: Drafter,
    db: Db,
    claude: Annotated[risk_agent.ClaudeClient, Depends(get_claude_client)],
    dry_run: bool = False,
) -> ClosureDraftResult:
    action = "draft"
    settings = get_settings()
    project = _project(db, project_id)
    report = rules.report_of(db, project.id)
    subject = _report_subject(report) if report is not None else None
    if settings.agent_mode == "off":
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal="agent_off",
            code=status.HTTP_409_CONFLICT,
            detail=AGENT_OFF,
            subject=subject,
        )
    _in_closure(db, user, project, action, subject)
    if report is None:
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal="no_report",
            code=status.HTTP_404_NOT_FOUND,
            detail=NO_REPORT,
        )
    common = {
        "mode": settings.agent_mode,
        "dry_run": dry_run,
        "model": settings.closure_agent_model,
        "prompt_version": closure_agent.PROMPT_VERSION,
    }
    if dry_run:
        data = closure_agent.request_data(db, report)
        api_request = closure_agent.build_request(data, settings)
        ceiling = closure_agent.cost_ceiling(api_request, settings)
        return ClosureDraftResult(
            **common,
            request=api_request,
            cost_ceiling_usd=ceiling.quantize(Decimal("0.01"), rounding=ROUND_UP),
        )
    save_it = settings.agent_mode == "enforce"
    try:
        result = closure_agent.run(
            db, claude, report, settings=settings, save_it=save_it, actor_user_id=user.id
        )
    except risk_agent.AgentError:
        logger.exception("closure agent run failed for project %s", project_id)
        _commit(db)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, AGENT_FAILED) from None
    _commit(db)
    return ClosureDraftResult(
        **common,
        sections=result.checked.sections,
        lessons=result.checked.lessons,
        lesson_ids=result.lesson_ids,
        figure_keys=result.checked.keys,
        replaced=result.replaced,
        kept=result.kept,
        report_version=report.version,
        audit_id=result.audit_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )


# --- Benefits, as a person measured them at close


@router.put("/{project_id}/closure-report/benefits/{benefit_id}", response_model=ClosureOut)
def measure_benefit(
    project_id: int, benefit_id: int, body: BenefitIn, user: BenefitWriter, db: Db
) -> ClosureOut:
    action = "measure_benefit"
    project = _locked_project(db, project_id)
    if benefit_id not in {b.id for b in rules.value_case_benefits(db, project.id)}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, BENEFIT_NOT_FOUND)
    row = db.scalar(
        select(ClosureBenefit).where(ClosureBenefit.benefit_id == benefit_id).with_for_update()
    )
    subject = (rules.BENEFIT_SUBJECT, benefit_id, row.version if row else 0)
    _in_closure(db, user, project, action, subject)
    if (row.version if row else 0) != body.version:
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal="stale_version",
            code=status.HTTP_409_CONFLICT,
            detail=STALE,
            subject=subject,
        )
    problem = None
    if (body.value_now is None) != (body.value_now_on is None):
        problem = VALUE_NEEDS_DATE
    elif (body.measure_owner_id is None) != (body.measure_by is None):
        problem = OWNER_NEEDS_DATE
    elif body.value_now is None and body.measure_owner_id is None:
        problem = NOTHING_ENTERED
    elif body.measure_owner_id is not None:
        owner = db.get(User, body.measure_owner_id)
        if owner is None or not owner.is_active:
            problem = BAD_OWNER
    if problem is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
    before = rules.benefit_snapshot(row) if row else None
    if row is None:
        row = ClosureBenefit(project_id=project.id, benefit_id=benefit_id)
        db.add(row)
    row.value_now = body.value_now
    row.value_now_on = body.value_now_on
    row.measure_owner_id = body.measure_owner_id
    row.measure_by = body.measure_by
    row.updated_by = user.id
    db.flush()
    rules.record(
        db,
        subject_type=rules.BENEFIT_SUBJECT,
        subject_id=benefit_id,
        version=row.version,
        project_id=project.id,
        actor_user_id=user.id,
        before=before,
        after=rules.benefit_snapshot(row),
        action="benefit_measured",
    )
    _commit(db)
    return _out(db, project)


# --- Lessons


def _lesson_subject(lesson: Lesson) -> tuple[str, int, int]:
    return (rules.LESSON_SUBJECT, lesson.id, lesson.version)


def _lessons_open(
    db: Session, user: User, project: Project, action: str, lesson: Lesson | None = None
) -> None:
    refused = rules.lesson_refusal(project)
    if refused is not None:
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal=refused[0],
            code=status.HTTP_409_CONFLICT,
            detail=refused[1],
            subject=_lesson_subject(lesson) if lesson is not None else None,
        )


def _locked_lesson(
    db: Session, user: User, project: Project, lesson_id: int, version: int, action: str
) -> Lesson:
    lesson = db.scalar(
        select(Lesson)
        .where(Lesson.id == lesson_id, Lesson.project_id == project.id)
        .with_for_update()
    )
    if lesson is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, LESSON_NOT_FOUND)
    _lessons_open(db, user, project, action, lesson)
    refusal = None
    if lesson.version != version:
        refusal = ("stale_version", STALE)
    elif lesson.status == DISCARDED:
        refusal = ("discarded", LESSON_DISCARDED)
    if refusal is not None:
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal=refusal[0],
            code=status.HTTP_409_CONFLICT,
            detail=refusal[1],
            subject=_lesson_subject(lesson),
        )
    return lesson


def _takeaway(body: LessonIn) -> None:
    if body.do_again is None and body.do_differently is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, NEEDS_TAKEAWAY)


def _record_lesson(
    db: Session, user: User, lesson: Lesson, before: dict[str, Any] | None, action: str
) -> None:
    rules.record(
        db,
        subject_type=rules.LESSON_SUBJECT,
        subject_id=lesson.id,
        version=lesson.version,
        project_id=lesson.project_id,
        actor_user_id=user.id,
        before=before,
        after=rules.lesson_snapshot(lesson),
        action=action,
    )


@router.post("/{project_id}/lessons", response_model=LessonOut, status_code=201)
def record_lesson(project_id: int, body: LessonIn, user: LessonWriter, db: Db) -> LessonOut:
    action = "record_lesson"
    project = _locked_project(db, project_id)
    _lessons_open(db, user, project, action)
    _takeaway(body)
    now = utcnow()
    lesson = Lesson(
        project_id=project.id,
        **body.model_dump(),
        source="person",
        status=RECORDED,
        created_by=user.id,
        reviewed_by=user.id,
        reviewed_at=now,
    )
    db.add(lesson)
    db.flush()
    _record_lesson(db, user, lesson, None, "lesson_recorded")
    _commit(db)
    return LessonOut.model_validate(lesson, from_attributes=True)


@router.patch("/{project_id}/lessons/{lesson_id}", response_model=LessonOut)
def edit_lesson(
    project_id: int, lesson_id: int, body: LessonEditIn, user: LessonEditor, db: Db
) -> LessonOut:
    """Edit a lesson; an agent's suggestion edited is recorded by the person who edited it."""
    action = "edit_lesson"
    project = _locked_project(db, project_id)
    lesson = _locked_lesson(db, user, project, lesson_id, body.version, action)
    _takeaway(body)
    before = rules.lesson_snapshot(lesson)
    for name, value in body.model_dump(exclude={"version"}).items():
        setattr(lesson, name, value)
    lesson.status = RECORDED
    lesson.reviewed_by = user.id
    lesson.reviewed_at = utcnow()
    db.flush()
    _record_lesson(db, user, lesson, before, "lesson_edited")
    _commit(db)
    return LessonOut.model_validate(lesson, from_attributes=True)


def _suggestion(
    db: Session, user: User, project: Project, lesson: Lesson, action: str, what: str
) -> None:
    if lesson.status != SUGGESTED:
        raise _refuse(
            db,
            user,
            project,
            action=action,
            refusal="not_suggested",
            code=status.HTTP_409_CONFLICT,
            detail=NOT_SUGGESTED.format(what=what),
            subject=_lesson_subject(lesson),
        )


@router.post("/{project_id}/lessons/{lesson_id}/record", response_model=LessonOut)
def confirm_lesson(
    project_id: int, lesson_id: int, body: LessonVersionIn, user: LessonRecorder, db: Db
) -> LessonOut:
    """A person records an agent's suggested lesson as it is."""
    action = "confirm_lesson"
    project = _locked_project(db, project_id)
    lesson = _locked_lesson(db, user, project, lesson_id, body.version, action)
    _suggestion(db, user, project, lesson, action, "recorded as it is")
    before = rules.lesson_snapshot(lesson)
    lesson.status = RECORDED
    lesson.reviewed_by = user.id
    lesson.reviewed_at = utcnow()
    db.flush()
    _record_lesson(db, user, lesson, before, "lesson_confirmed")
    _commit(db)
    return LessonOut.model_validate(lesson, from_attributes=True)


@router.post("/{project_id}/lessons/{lesson_id}/discard", response_model=LessonOut)
def discard_lesson(
    project_id: int, lesson_id: int, body: LessonVersionIn, user: LessonDiscarder, db: Db
) -> LessonOut:
    """A person discards an agent's suggested lesson; it stays on the record as discarded."""
    action = "discard_lesson"
    project = _locked_project(db, project_id)
    lesson = _locked_lesson(db, user, project, lesson_id, body.version, action)
    _suggestion(db, user, project, lesson, action, "discarded")
    before = rules.lesson_snapshot(lesson)
    lesson.status = DISCARDED
    lesson.reviewed_by = user.id
    lesson.reviewed_at = utcnow()
    db.flush()
    _record_lesson(db, user, lesson, before, "lesson_discarded")
    _commit(db)
    return LessonOut.model_validate(lesson, from_attributes=True)
