"""Shared fixtures for the security suite (item 2.4): a fresh SQLite schema per test and a client.

`sign_in` and the login rate-limiter reset come from `tests/conftest.py`.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import engine
from app.main import app
from app.models import Base, User


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture
def web(db, gates_policy) -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def target(db) -> User:
    """A project manager that an admin may act on."""
    user = User(
        email="target@pmo.example",
        display_name="Target",
        password_hash="not-a-real-hash",
        role="project_manager",
    )
    db.add(user)
    db.commit()
    return user
