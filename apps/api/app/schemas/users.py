"""API shapes for managing users."""

from pydantic import BaseModel, ConfigDict

from app.authz import Role


class RoleChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
