"""API shapes for managing users."""

from pydantic import BaseModel, ConfigDict

from app.authz import Role


class RoleChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role


class PersonOut(BaseModel):
    """An active user as anyone who reads projects sees them: a name to pick as a risk owner.
    No email, role or sign-in details."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str
