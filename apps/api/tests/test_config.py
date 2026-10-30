import pytest
from pydantic import ValidationError
from sqlalchemy import make_url

from app.config import AGENT_MODES, Settings

SECRET = "jwt-not-a-real-secret"


@pytest.fixture
def clean_env(monkeypatch):
    """No app settings in the environment; each test sets what it needs."""
    for name in ("APP_ENV", "DATABASE_URL", "JWT_SECRET", "DB_HOST", "DB_USER", "DB_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_settings_load_from_env(clean_env):
    clean_env.setenv("DATABASE_URL", "sqlite:///./env.db")
    clean_env.setenv("JWT_SECRET", SECRET)
    clean_env.setenv("AGENT_MODE", "shadow")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./env.db"
    assert settings.jwt_secret == SECRET
    assert settings.agent_mode == "shadow"
    assert settings.app_env == "dev"


def test_settings_load_from_dotenv_file(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATABASE_URL=sqlite:///./dotenv.db\nJWT_SECRET={SECRET}\n"
        "CORS_ORIGINS=http://a.example, http://b.example\n"
    )
    settings = Settings(_env_file=env_file)
    assert settings.database_url == "sqlite:///./dotenv.db"
    assert settings.cors_origin_list == ["http://a.example", "http://b.example"]


def test_agent_mode_defaults_off_and_is_a_closed_set(clean_env):
    assert Settings(_env_file=None, app_env="test").agent_mode == "off"
    assert AGENT_MODES == ("off", "shadow", "enforce")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="test", agent_mode="auto")


def test_missing_jwt_secret_fails_fast_outside_tests(clean_env):
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None, database_url="sqlite:///./x.db")


def test_missing_database_url_fails_fast_outside_tests(clean_env):
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(_env_file=None, jwt_secret=SECRET)


def test_validation_error_does_not_echo_secrets(clean_env):
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, db_password="pw-must-not-leak", anthropic_api_key="key-hidden")
    assert "pw-must-not-leak" not in str(excinfo.value)
    assert "key-hidden" not in str(excinfo.value)


def test_test_env_needs_neither_and_defaults_to_sqlite(clean_env):
    settings = Settings(_env_file=None, app_env="test")
    assert settings.database_url == "sqlite:///./test.db"
    assert settings.jwt_secret is None


def test_repr_hides_secrets(clean_env):
    settings = Settings(
        _env_file=None,
        database_url="mysql+pymysql://user:hidden-pw@db/pragmattie_pmo",
        jwt_secret="hidden-jwt",
        anthropic_api_key="hidden-key",
        database_ca_cert="-----BEGIN CERTIFICATE-----hidden-ca",
        db_password="hidden-db-pw",
    )
    text = repr(settings) + str(settings)
    for secret in ("hidden-pw", "hidden-jwt", "hidden-key", "hidden-ca", "hidden-db-pw"):
        assert secret not in text


def test_url_built_from_parts_survives_special_characters(clean_env):
    password = "p@ss:w/rd#1?%"
    settings = Settings(
        _env_file=None,
        jwt_secret=SECRET,
        db_host="db",
        db_port=3306,
        db_user="pmo",
        db_password=password,
        db_name="pragmattie_pmo",
    )
    url = make_url(settings.database_url)
    assert url.drivername == "mysql+pymysql"
    assert url.password == password
    assert (url.username, url.host, url.port, url.database) == ("pmo", "db", 3306, "pragmattie_pmo")


def test_full_database_url_wins_over_parts(clean_env):
    settings = Settings(
        _env_file=None, jwt_secret=SECRET, database_url="sqlite:///./x.db", db_host="db"
    )
    assert settings.database_url == "sqlite:///./x.db"
