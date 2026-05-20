"""
database/db.py
--------------
Database setup and connection management for the grant
prospecting agent portal.

Uses SQLAlchemy with SQLite for development.
Switch to PostgreSQL for production by changing DATABASE_URL
in the .env file:

    Development:  sqlite:///./grant_prospector.db
    Production:   postgresql://user:password@host/dbname

All portal models import Base from this file and all
portal routes import get_db from this file.

Usage:
    from database.db import get_db, Base, engine

    # In a FastAPI route:
    def my_route(db: Session = Depends(get_db)):
        results = db.query(MyModel).all()
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
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

# ─────────────────────────────────────────────────────────────────────────────
# Engine
# The engine is the connection to the database.
# connect_args is SQLite-specific — allows multiple threads to
# share the same connection (needed for FastAPI's async nature).
# ─────────────────────────────────────────────────────────────────────────────

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args = {"check_same_thread": False},
        echo         = False,  # Set to True to log all SQL queries
    )

    # Enable WAL mode for SQLite — better concurrent read performance
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

else:
    # PostgreSQL or other databases
    engine = create_engine(DATABASE_URL, echo=False)

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
    # Import all models here so Base knows about them
    from portal.models import user, result, learning  # noqa: F401

    Base.metadata.create_all(bind=engine)
    print(f"[Database] Tables created — {DATABASE_URL}")


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