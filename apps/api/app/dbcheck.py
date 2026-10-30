"""Connection smoke test: `python -m app.dbcheck`.

Prints the server version and whether the connection uses TLS. It never prints the URL or any
credential, and an unreachable server gives a short message and exit code 1, not a traceback.
"""

import sys

from pydantic import ValidationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.config import Settings, get_settings


def _version_and_tls(conn: Connection) -> tuple[str, str]:
    if conn.dialect.name == "sqlite":
        return f"SQLite {conn.scalar(text('select sqlite_version()'))}", "not applicable (SQLite)"
    version = conn.scalar(text("select version()"))
    row = conn.execute(text("show session status like 'Ssl_cipher'")).first()
    cipher = row[1] if row else ""
    tls = f"on ({cipher})" if cipher else "off"
    return f"{conn.dialect.name} {version}", tls


def run(settings: Settings) -> int:
    # Imported here: app.db builds its engine from get_settings() on import.
    from app.db import make_engine

    engine = make_engine(settings)
    try:
        with engine.connect() as conn:
            version, tls = _version_and_tls(conn)
    except DBAPIError as exc:
        code = exc.orig.args[0] if exc.orig is not None and exc.orig.args else None
        detail = f" (driver error {code})" if isinstance(code, int) else ""
        print(
            f"Database unreachable{detail}: the server did not accept the connection. "
            "The hosted free tier powers off when idle; start it and try again.",
            file=sys.stderr,
        )
        return 1
    except SQLAlchemyError as exc:
        print(f"Database check failed: {type(exc).__name__}.", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(f"Server version: {version}")
    print(f"TLS: {tls}")
    return 0


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        messages = "; ".join(error["msg"] for error in exc.errors(include_input=False))
        print(f"Settings are incomplete: {messages}", file=sys.stderr)
        return 1
    return run(settings)


if __name__ == "__main__":
    sys.exit(main())
