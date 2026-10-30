import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app import dbcheck
from app.config import Settings
from app.db import connect_args, get_db
from app.models import utcnow

API_DIR = Path(__file__).resolve().parents[1]
PEM = "-----BEGIN CERTIFICATE-----\nMIIBdummy\n-----END CERTIFICATE-----\n"
MYSQL_URL = "mysql+pymysql://pmo:pw@db.example:3306/pragmattie_pmo"


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, app_env="test", **kwargs)


def test_no_tls_without_a_ca():
    assert connect_args(_settings(database_url=MYSQL_URL)) == {}


def test_tls_with_a_ca_path():
    args = connect_args(_settings(database_url=MYSQL_URL, database_ca_cert="/etc/ssl/ca.pem"))
    assert args == {"ssl": {"ca": "/etc/ssl/ca.pem"}}


def test_tls_with_pem_text_writes_a_file():
    args = connect_args(_settings(database_url=MYSQL_URL, database_ca_cert=PEM))
    path = Path(args["ssl"]["ca"])
    assert path.read_text() == PEM


def test_ca_is_ignored_on_sqlite():
    assert connect_args(_settings(database_url="sqlite:///./x.db", database_ca_cert=PEM)) == {}


def test_get_db_yields_a_working_session():
    gen = get_db()
    session = next(gen)
    assert session.connection().exec_driver_sql("select 1").scalar() == 1
    gen.close()


def test_utcnow_is_naive_utc():
    now = utcnow()
    assert now.tzinfo is None
    expected = datetime.now(UTC).replace(tzinfo=None)
    assert abs((expected - now).total_seconds()) < 5


def test_dbcheck_reports_version_and_tls_on_sqlite(tmp_path, capsys):
    assert dbcheck.run(_settings(database_url=f"sqlite:///{tmp_path / 'c.db'}")) == 0
    out = capsys.readouterr().out
    assert "Server version: SQLite" in out
    assert "TLS: not applicable" in out


def test_dbcheck_unreachable_server_is_clean_and_hides_credentials(tmp_path):
    # Port 1 on localhost refuses at once: a stand-in for a powered-off hosted database.
    env = {
        "APP_ENV": "test",
        "DATABASE_URL": "mysql+pymysql://secretuser:s3cret-pw@127.0.0.1:1/pragmattie_pmo",
        "PATH": "",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.dbcheck"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "Database unreachable" in output
    assert "Traceback" not in output
    for leak in ("s3cret-pw", "secretuser", "mysql+pymysql"):
        assert leak not in output


def test_dbcheck_reports_incomplete_settings_without_traceback(tmp_path):
    env = {"APP_ENV": "prod", "DB_PASSWORD": "s3cret-pw", "PATH": ""}
    result = subprocess.run(
        [sys.executable, "-m", "app.dbcheck"],
        cwd=tmp_path,
        env={**env, "PYTHONPATH": str(API_DIR)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "JWT_SECRET" in output
    assert "Traceback" not in output
    assert "s3cret-pw" not in output


def test_dbcheck_reports_a_tls_verification_failure(monkeypatch, capsys):
    import ssl

    import pymysql
    from sqlalchemy.exc import OperationalError

    driver_error = pymysql.err.OperationalError(2003, "Can't connect to MySQL server")
    driver_error.original_exception = ssl.SSLCertVerificationError("certificate verify failed")

    class _Engine:
        def connect(self):
            raise OperationalError("connect", {}, driver_error)

        def dispose(self):
            pass

    monkeypatch.setattr("app.db.make_engine", lambda settings: _Engine())
    assert dbcheck.run(_settings(database_url=MYSQL_URL, database_ca_cert=PEM)) == 1
    err = capsys.readouterr().err
    assert "TLS failed" in err
    assert "mysql+pymysql" not in err
