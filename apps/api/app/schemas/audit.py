"""API shape of an audit row."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict

from app.schemas.common import MoneyStr


def _to_cents(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return value


# Costs are stored to 6 places; the API shows them as money (a 2-decimal string).
CostStr = Annotated[MoneyStr, BeforeValidator(_to_cents)]


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
    cost_usd: CostStr | None
    error: str | None
    supersedes_id: int | None
    actor_user_id: int | None
    created_at: datetime
