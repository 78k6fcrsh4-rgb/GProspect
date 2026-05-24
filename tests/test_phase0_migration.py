"""
tests/test_phase0_migration.py
------------------------------
Idempotency tests for the Phase 0 lightweight migration helper.

run_lightweight_migrations() is called on every portal startup. It must be
safe to call repeatedly without raising errors and without re-applying the
same ALTER twice. These tests pin that contract.

We don't use the TestClient here — we drive the engine directly so we can
swap the module-level `engine` and assert against the inspector.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text


def _bind_test_engine(test_engine):
    """
    Re-point database.db's module-level `engine` at the test engine so
    run_lightweight_migrations() operates on our temp DB instead of the
    default sqlite:///./grant_prospector.db.

    Returns the original engine so the caller can restore it.
    """
    import database.db as db
    original = db.engine
    db.engine = test_engine
    return original


@pytest.fixture()
def db_module_with_test_engine(engine):
    """Bind database.db.engine to the test engine for the duration of the test."""
    import database.db as db
    original_engine = db.engine
    db.engine = engine
    try:
        yield db
    finally:
        db.engine = original_engine


def test_create_tables_then_migrations_idempotent(db_module_with_test_engine, engine):
    """
    Running create_tables() + run_lightweight_migrations() twice in a row
    against a fresh database must produce no errors and no schema drift.
    """
    db_module_with_test_engine.create_tables()
    db_module_with_test_engine.run_lightweight_migrations()

    snap1 = _schema_snapshot(engine)

    # Second pass — should be a no-op for the schema.
    db_module_with_test_engine.create_tables()
    db_module_with_test_engine.run_lightweight_migrations()

    snap2 = _schema_snapshot(engine)

    assert snap1 == snap2, (
        "Schema drifted between runs of run_lightweight_migrations(). "
        "The migration helper must be idempotent."
    )


def test_migration_adds_token_version_on_legacy_db(db_module_with_test_engine, engine):
    """
    Simulate a pre-Round-3 database (no token_version column) and verify the
    migration adds the column.
    """
    # Build a legacy users table without token_version.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users ("
            " id INTEGER PRIMARY KEY,"
            " email TEXT UNIQUE NOT NULL,"
            " full_name TEXT NOT NULL,"
            " org_name TEXT NOT NULL,"
            " hashed_password TEXT NOT NULL,"
            " role TEXT NOT NULL,"
            " is_active INTEGER NOT NULL DEFAULT 1,"
            " is_verified INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT,"
            " updated_at TEXT,"
            " last_login TEXT,"
            " reset_token TEXT,"
            " reset_token_expires_at TEXT"
            ")"
        ))

    inspector = inspect(engine)
    assert "token_version" not in {c["name"] for c in inspector.get_columns("users")}

    db_module_with_test_engine.run_lightweight_migrations()

    inspector = inspect(engine)
    assert "token_version" in {c["name"] for c in inspector.get_columns("users")}


def test_migration_adds_org_id_and_backfills(db_module_with_test_engine, engine):
    """
    Simulate a v1-era database with a users row that has org_name but no
    org_id. After creating the organizations table and running the migration,
    the users.org_id should be backfilled to the matching org's primary key.
    """
    # Create a minimal legacy users table (no org_id).
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users ("
            " id INTEGER PRIMARY KEY,"
            " email TEXT UNIQUE NOT NULL,"
            " full_name TEXT NOT NULL,"
            " org_name TEXT NOT NULL,"
            " hashed_password TEXT NOT NULL,"
            " role TEXT NOT NULL,"
            " token_version INTEGER NOT NULL DEFAULT 0,"
            " is_active INTEGER NOT NULL DEFAULT 1,"
            " is_verified INTEGER NOT NULL DEFAULT 0"
            ")"
        ))
        conn.execute(text(
            "INSERT INTO users (email, full_name, org_name, hashed_password, role) "
            "VALUES ('admin@deborahsplace.org', 'Mary Kelly', \"Deborah's Place\", 'x', 'admin')"
        ))

    # Create the v2 tables (organizations etc.) via create_tables on the legacy DB.
    db_module_with_test_engine.create_tables()

    # Seed an Organization row that matches the user's org_name.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organizations (slug, display_name, status, settings, created_at, updated_at) "
            "VALUES ('deborahs-place', \"Deborah's Place\", 'ACTIVE', '{}', "
            "        datetime('now'), datetime('now'))"
        ))

    # Now migrate — should add org_id (if not already) and backfill from org_name.
    db_module_with_test_engine.run_lightweight_migrations()

    # Verify the column exists and the user got backfilled.
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT u.org_id, o.slug "
            "FROM users u JOIN organizations o ON o.id = u.org_id "
            "WHERE u.email = 'admin@deborahsplace.org'"
        )).fetchone()

    assert row is not None,            "org_id was not backfilled — JOIN returned no row"
    assert row[1] == "deborahs-place", f"org_id pointed at wrong org: slug={row[1]}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _schema_snapshot(engine) -> dict:
    """
    Returns a tuple-of-tuples description of every (table, column) pair so
    the snapshot can be == compared across runs. Strips defaults that SQLite
    formats inconsistently across operations (e.g. boolean → integer 1/0).
    """
    inspector = inspect(engine)
    snap = {}
    for table_name in sorted(inspector.get_table_names()):
        cols = inspector.get_columns(table_name)
        snap[table_name] = tuple(sorted(
            (c["name"], str(c["type"]).upper()) for c in cols
        ))
    return snap
