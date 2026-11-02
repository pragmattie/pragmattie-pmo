"""API shapes for signing in. No shape here ever carries a password hash or a token."""

from pydantic import BaseModel, ConfigDict

# Longer passwords are refused before argon2 sees them.
PASSWORD_MAX_LENGTH = 256


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    role: str
    # What the role may do (app/authz.py), so the web can hide the rest. The API still checks.
    actions: list[str] = []
