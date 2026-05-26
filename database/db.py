"""
database/db.py
--------------
Database setup and connection management for the grant
prospecting agent portal.

Two engines supported:
    sqlite:///./grant_prospector.db                     ← dev + tests
    postgresql+psycopg://user:pass@host:5432/dbname     ← v2 prod (Phase 3+)

The bare `postgresql://...` URL is also accepted; SQLAlchemy maps it
to its default driver (psycopg2). The `+psycopg` suffix targets the
newer psycopg3 we pin in requirements.txt.

Choose by setting DATABASE_URL in .env. Default is SQLite so zero-config
local dev still works.

All portal models import Base from this file and all portal routes
import get_db from this file.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Database URL
# Reads from .env file. Defaults to SQLite in the project root.
# ─────────────────────────────────────────────────────────────────────────────

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./grant_prospector.db"
)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _is_postgres(url: str) -> bool:
    return url.startswith("postgres")


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# Dialect-specific kwargs:
#   - SQLite: needs `check_same_thread=False` for FastAPI's threadpool.
#     Also: enable WAL + foreign_keys at connect-time via a PRAGMA hook.
#   - Postgres: connection pool sized for a single-worker dev portal.
#     pool_pre_ping detects + recycles stale connections after a long idle.
# ─────────────────────────────────────────────────────────────────────────────

if _is_sqlite(DATABASE_URL):
    engine = create_engine(
        DATABASE_URL,
        connect_args = {"check_same_thread": False},
        echo         = False,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

else:
    # Postgres (or anything else SQLAlchemy supports).
    engine = create_engine(
        DATABASE_URL,
        pool_size     = 5,
        max_overflow  = 5,
        pool_pre_ping = True,
        echo          = False,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Session factory
# SessionLocal is used to create database sessions.
# Each request gets its own session that is closed when done.
# ─────────────────────────────────────────────────────────────────────────────

SessionLocal = sessionmaker(
    autocommit = False,
    autoflush  = False,
    bind       = engine,
)

# ─────────────────────────────────────────────────────────────────────────────
# Base
# All database models inherit from Base.
# Base keeps track of all models so create_all() can create
# their tables automatically.
# ─────────────────────────────────────────────────────────────────────────────

Base = declarative_base()

# ─────────────────────────────────────────────────────────────────────────────
# Dependency
# get_db is used in FastAPI routes as a dependency injection.
# It provides a database session and ensures it is closed
# after the request completes — even if an error occurs.
# ─────────────────────────────────────────────────────────────────────────────

def get_db():
    """
    FastAPI dependency that provides a database session.

    Yields a session and ensures it is closed after use.
    Use with FastAPI's Depends() in route functions.

    Example:
        @app.get("/results")
        def get_results(db: Session = Depends(get_db)):
            return db.query(GrantResult).all()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables() -> None:
    """
    Creates all database tables defined in the models.

    Called once at application startup. Safe to call multiple
    times — only creates tables that do not already exist.

    All model files must be imported before calling this
    so SQLAlchemy knows about them.
    """
    # Import all models here so Base knows about them.
    # Order matters: organization first because user/result/learning/profile/
    # opportunity/funder_candidate all have FKs into it. The grant module's
    # Funder / RecipientOrg / Grant are global (no org_id FK).
    from portal.models import (                                   # noqa: F401
        organization, org_profile, user, result, learning, opportunity,
        funder_candidate, grant, capacity,
    )

    Base.metadata.create_all(bind=engine)
    print(f"[Database] Tables created — {DATABASE_URL}")


def run_lightweight_migrations() -> None:
    """
    Idempotent schema patches for known additive changes.

    Bridges the gap until Alembic is introduced. `Base.metadata.create_all`
    only creates *missing* tables — it does NOT add new columns to existing
    tables, and it doesn't backfill data. This helper handles both, only
    when the work hasn't been done, so it's safe to call on every startup
    and a no-op on fresh databases.

    Replace with proper migrations (Alembic) before any non-trivial schema
    change. Adds here should be paired with a matching column on the model
    so create_all handles fresh DBs.

    Phase-0-specific additions (v2):
      - users.token_version (token revocation)         [shipped earlier]
      - organizations table                            [Phase 0]
      - org_profile_versions table                     [Phase 0]
      - users.org_id FK + backfill from users.org_name [Phase 0]
      - grant_results.org_id FK + backfill             [Phase 0]
      - learning_entries.org_id FK + backfill          [Phase 0]
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    # ── users.token_version (round-3 hardening — pre-v2) ──────────────────────
    if "users" in table_names:
        existing_cols = {c["name"] for c in inspector.get_columns("users")}
        if "token_version" not in existing_cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE users "
                    "ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
                ))
            print("[Database] Migration: added users.token_version")
            inspector = inspect(engine)  # refresh after ALTER

    # ── Phase 0: org_id FK on tenant-scoped tables ────────────────────────────
    # We add the column as nullable so the ALTER succeeds on existing rows,
    # backfill org_id from org_name (matching organization.display_name),
    # then leave it nullable at the SQL level — application code enforces
    # not-null via the model. (SQLite doesn't support ALTER COLUMN to add
    # NOT NULL after the fact; Postgres would, but we'll handle that when
    # we migrate.)
    _add_org_id_to_table(inspector, "users")
    _add_org_id_to_table(inspector, "grant_results")
    _add_org_id_to_table(inspector, "learning_entries")


def _add_org_id_to_table(inspector, table_name: str) -> None:
    """
    Idempotent helper: adds `org_id INTEGER` to the named table if missing,
    then backfills any NULL org_id rows by matching org_name against
    organizations.display_name. Safe on fresh databases (no-op when column
    already exists or table doesn't exist).

    SQL syntax used here is the subset that's compatible across both
    SQLite and Postgres — plain ALTER TABLE ... ADD COLUMN, correlated
    subquery UPDATE.
    """
    from sqlalchemy import text

    if table_name not in set(inspector.get_table_names()):
        return

    existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
    if "org_id" not in existing_cols:
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN org_id INTEGER"
            ))
        print(f"[Database] Migration: added {table_name}.org_id")

    # Backfill — works whether the column was just added or has existed for a
    # while but contains NULLs. Both SQLite and Postgres accept this
    # correlated-subquery UPDATE syntax.
    if "organizations" in set(inspector.get_table_names()):
        with engine.begin() as conn:
            result = conn.execute(text(
                f"UPDATE {table_name} "
                f"   SET org_id = ("
                f"       SELECT id FROM organizations "
                f"        WHERE organizations.display_name = {table_name}.org_name"
                f"   ) "
                f" WHERE org_id IS NULL AND org_name IS NOT NULL"
            ))
            if result.rowcount:
                print(
                    f"[Database] Migration: backfilled {result.rowcount} "
                    f"{table_name}.org_id rows from org_name"
                )


def get_db_stats() -> dict:
    """
    Returns basic database statistics.

    Used by the admin portal dashboard to show database health.

    Returns:
        Dictionary with table names and row counts.
    """
    from sqlalchemy import inspect, text

    inspector   = inspect(engine)
    table_names = inspector.get_table_names()
    stats       = {"database_url": DATABASE_URL, "tables": {}}

    with engine.connect() as conn:
        for table in table_names:
            try:
                result = conn.execute(
                    text(f"SELECT COUNT(*) FROM {table}")
                )
                count = result.scalar()
                stats["tables"][table] = count
            except Exception:
                stats["tables"][table] = "error"

    return stats