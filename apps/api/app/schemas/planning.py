"""API shapes of a project, its brief, stakeholders and assumptions (item 4.6).

Every input refuses unknown fields. Edits carry the `version` the client read; a stale one is a
409. A PATCH changes only the fields it sends; sending `null` clears an optional field.
"""

from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.intake import LONG_TEXT_MAX
from app.models.planning import (
    ASSUMPTION_MAX,
    DECISION_RIGHTS_MAX,
    HOW_OFTEN_MAX,
    INTEREST_MAX,
    NAME_MAX,
    NEEDS_MAX,
)
from app.schemas.common import MoneyStr

LongText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LONG_TEXT_MAX)
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)]
Interest = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=INTEREST_MAX)
]
Needs = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NEEDS_MAX)]
HowOften = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=HOW_OFTEN_MAX)
]
AssumptionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=ASSUMPTION_MAX)
]
RoleTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
RoleName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Budget = Annotated[MoneyStr, Field(ge=0)]
AssumptionStatus = Literal["open", "confirmed", "wrong"]
Version = Annotated[int, Field(ge=1)]


class ProjectDetailOut(BaseModel):
    """A project, with its client's name and the request it came from (if any)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int | None
    client_name: str | None = None
    name: str
    stage: str
    status: str
    sponsor: str
    sponsor_user_id: int | None
    pm: str
    pm_user_id: int | None
    request_id: int | None = None
    created_at: datetime


class DecisionRight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: RoleName
    title: RoleTitle
    decides: Needs


class BriefUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Version
    purpose: LongText | None = None
    objectives: LongText | None = None
    scope_in: LongText | None = None
    scope_out: LongText | None = None
    wanted_by: date | None = None
    budget_low: Budget | None = None
    budget_high: Budget | None = None
    decision_rights: list[DecisionRight] | None = Field(
        default=None, max_length=DECISION_RIGHTS_MAX
    )

    @model_validator(mode="after")
    def _required_stay_set(self) -> Self:
        for name in ("purpose", "objectives", "decision_rights"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} can't be cleared")
        return self


class BriefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    purpose: str
    objectives: str
    scope_in: str | None
    scope_out: str | None
    wanted_by: date | None
    budget_low: MoneyStr | None
    budget_high: MoneyStr | None
    decision_rights: list[DecisionRight]
    sponsor: str
    sponsor_user_id: int | None
    pm: str
    pm_user_id: int | None
    version: int
    updated_at: datetime


class StakeholderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    user_id: int | None = None
    role_or_interest: Interest
    needs_to_hear: Needs | None = None
    how_often: HowOften | None = None


class StakeholderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Version
    name: Name | None = None
    user_id: int | None = None
    role_or_interest: Interest | None = None
    needs_to_hear: Needs | None = None
    how_often: HowOften | None = None

    @model_validator(mode="after")
    def _required_stay_set(self) -> Self:
        for name in ("name", "role_or_interest"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} can't be cleared")
        return self


class StakeholderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    user_id: int | None
    role_or_interest: str
    needs_to_hear: str | None
    how_often: str | None
    version: int


class AssumptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: AssumptionText
    owner_user_id: int
    confirm_by: date
    status: AssumptionStatus = "open"


class AssumptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Version
    text: AssumptionText | None = None
    owner_user_id: int | None = None
    confirm_by: date | None = None
    status: AssumptionStatus | None = None

    @model_validator(mode="after")
    def _required_stay_set(self) -> Self:
        for name in ("text", "owner_user_id", "confirm_by", "status"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} can't be cleared")
        return self


class AssumptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    text: str
    owner_user_id: int
    owner_name: str | None = None
    confirm_by: date
    status: str
    version: int
