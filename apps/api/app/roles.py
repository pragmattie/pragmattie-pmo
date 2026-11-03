"""The roles a user can hold. Kept apart from `app.authz` so the policy loader can check role
names without loading the database."""

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    SPONSOR = "sponsor"
    PROJECT_MANAGER = "project_manager"
    ETHICS_REVIEWER = "ethics_reviewer"
    AUDITOR = "auditor"
