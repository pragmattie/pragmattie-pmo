import os
import tempfile

import pytest

# Tests use a throwaway SQLite file, set before the app is imported.
_db_dir = tempfile.mkdtemp()
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_db_dir, 'test.db')}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
