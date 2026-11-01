"""API shapes of clients and projects."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    industry: str | None
    website: str | None
    contact_name: str | None
    contact_email: str | None
    created_at: datetime


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int | None
    name: str
    stage: str
    sponsor: str
    sponsor_email: str | None
    pm: str
    pm_email: str | None
    status: str
    created_at: datetime
