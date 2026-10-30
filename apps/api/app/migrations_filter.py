"""Table-name constants for a shared hosted database.

If the hosted database is shared with another app, set TABLE_PREFIX (for example "pmo_x_") and
every table, plus Alembic's version table, gets it; Alembic then ignores tables without it.
The default is no prefix.
"""

TABLE_PREFIX = ""
VERSION_TABLE = f"{TABLE_PREFIX}alembic_version"


def table_name(name: str) -> str:
    return f"{TABLE_PREFIX}{name}"


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """Alembic `include_name` hook: compare only this app's tables."""
    if type_ == "table":
        return name is not None and name.startswith(TABLE_PREFIX)
    return True
