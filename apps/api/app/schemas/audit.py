"""API shape of an audit row."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.common import MoneyStr


class AuditDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent: str
    trigger: str
    subject_type: str
    subject_id: str
    version: int
    attempt: int
    status: str
    tier: str | None
    signals: Any | None
    output: Any | None
    action_taken: Any | None
    human_override: Any | None
    model: str | None
    prompt_version: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: MoneyStr | None
    error: str | None
    supersedes_id: int | None
    actor_user_id: int | None
    created_at: datetime
