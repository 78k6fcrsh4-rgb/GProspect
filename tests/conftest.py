"""
tests/conftest.py
-----------------
Shared pytest fixtures for GProspect v2 tests.

Provides:
  - `engine`            — a fresh in-memory-ish SQLite engine per test
  - `db_session`        — a SQLAlchemy session bound to that engine
  - `client`            — a FastAPI TestClient with get_db dependency
                          overridden to use the test session
  - `auth_token_for`    — helper that creates a user + returns a JWT
                          ready to drop into an Authorization header

Each test gets its own clean SQLite file in a tmp_path so test order
doesn't matter and parallel runs don't collide.

These fixtures don't seed any data. Tests that want orgs/users create
them explicitly so the test reads as a clear narrative.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

# Ensure environment is sane before importing the app — disable the noisy
# ephemeral-SECRET_KEY warning by pre-setting one. Use a known string for
# determinism; never use this outside the test suite.
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-anywhere-else")
os.environ.setdefault("CORS_ALLOW_ALL", "1")


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """
    The /auth/login rate limit (5/min/IP) is global across tests within the
    same pytest process. Without resetting, the 9th login across the test
    suite hits 429. Reset between tests so each test starts clean.
    """
    from portal.limiter import limiter
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture()
def engine(tmp_path):
    """Per-test SQLite engine in a tmp_path-scoped file."""
    db_path = tmp_path / "test.db"
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args = {"check_same_thread": False},
        echo         = False,
    )

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return eng


@pytest.fixture()
def db_session(engine) -> Iterator[Session]:
    """Per-test SQLAlchemy session bound to the test engine."""
    # Import Base + all models so create_all knows them.
    from database.db import Base
    from portal.models import (                            # noqa: F401
        organization, org_profile, user, result, learning,
    )
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(engine) -> Iterator[TestClient]:
    """
    FastAPI TestClient with the production get_db dependency overridden
    to read/write the test engine.

    Deliberately does NOT use TestClient as a context manager — that would
    trigger the lifespan handler, which calls create_tables() and
    _seed_initial_orgs() against the PRODUCTION engine (the one bound at
    module import of database.db), not the test engine. Tests construct
    their own orgs/users explicitly, so lifespan-side seeding is a
    misdirection here.
    """
    from database.db import Base, get_db
    from portal.models import (                            # noqa: F401
        organization, org_profile, user, result, learning,
    )

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_get_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    from portal.main import app
    app.dependency_overrides[get_db] = _override_get_db
    try:
        # No `with` → no lifespan → no production-DB seeding side effects.
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience builders — call from tests to set up fixtures inline.
# ─────────────────────────────────────────────────────────────────────────────

def make_org(db_session, slug: str, display_name: str):
    """Insert an Organization row and return it."""
    from portal.models.organization import Organization, OrgStatus
    org = Organization(
        slug         = slug,
        display_name = display_name,
        status       = OrgStatus.ACTIVE,
        settings     = {},
    )
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


def make_user(db_session, *, email: str, org, role: str = "admin",
              password: str = "password123!"):
    """Insert a User row tied to the given Organization. Returns (user, password)."""
    from portal.models.user    import User, UserRole
    from portal.auth.security  import hash_password

    u = User(
        email           = email,
        full_name       = email.split("@")[0].title(),
        org_id          = org.id,
        org_name        = org.display_name,
        hashed_password = hash_password(password),
        role            = UserRole(role),
        is_active       = True,
        is_verified     = True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u, password
