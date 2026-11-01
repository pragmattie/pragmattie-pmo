"""People who sign in, and the session tokens they have signed out of.

`email` is stored in lower case and compared in lower case. `role` is one of the roles in
`app/authz.py`, which also says what each role may do. `failed_logins` and `locked_until` are kept
for lockout (item 2.3) and not yet used.

A row in `revoked_tokens` holds a signed-out token's `jti` until the token would have expired
anyway; expired rows are deleted on logout so the table stays small.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.migrations_filter import table_name
from app.models.base import Base, utcnow

USERS_TABLE = table_name("users")
REVOKED_TOKENS_TABLE = table_name("revoked_tokens")


def normalize_email(value: str) -> str:
    """Emails are stored and compared in lower case, without surrounding spaces."""
    return value.strip().lower()


class User(Base):
    __tablename__ = USERS_TABLE
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(default=True)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    @validates("email")
    def _lower_email(self, _key: str, value: str) -> str:
        return normalize_email(value)

    def __repr__(self) -> str:
        # Never include the password hash.
        return f"User(id={self.id!r}, email={self.email!r}, role={self.role!r})"


class RevokedToken(Base):
    __tablename__ = REVOKED_TOKENS_TABLE
    __table_args__ = (Index("ix_revoked_tokens_expires_at", "expires_at"),)

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
