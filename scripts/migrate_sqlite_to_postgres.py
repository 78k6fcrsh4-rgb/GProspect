#!/usr/bin/env python3
"""
scripts/migrate_sqlite_to_postgres.py
-------------------------------------
One-shot move of existing GProspect data from a local SQLite database
(grant_prospector.db) into a Postgres instance.

Use this on your Mac after running `docker-compose up -d postgres` and
updating DATABASE_URL in .env to point at Postgres. Idempotent: tables
that already have rows on the Postgres side are skipped, so re-running
the script is safe.

Run:
    cd ~/WorkBench/AI4GSH/lsrmba777
    python3 scripts/migrate_sqlite_to_postgres.py \\
        --sqlite ./grant_prospector.db \\
        --postgres postgresql+psycopg://gprospect:gprospect@localhost:5432/gprospect

Or with the env defaults:
    DATABASE_URL=postgresql+psycopg://gprospect:gprospect@localhost:5432/gprospect \\
        python3 scripts/migrate_sqlite_to_postgres.py

What it copies (in dependency order — FK-safe):
    organizations, users, org_profile_versions, opportunity_pursuits,
    opportunity_narratives, funder_candidates, grant_results,
    learning_entries, funders, recipient_orgs, grants.

What it doesn't copy:
    Tracked junk that pre-dates v2 (none expected at this point).

If a target table already has rows, the script logs and skips it. If
the source table is missing, it's silently skipped (older SQLite DBs
may not have every v2 table).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make the project importable when running this as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

log = logging.getLogger("migrate")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# Order matters: parent rows first so FK constraints are satisfied.
TABLE_ORDER = [
    "organizations",
    "users",
    "org_profile_versions",
    "grant_results",
    "learning_entries",
    "opportunity_pursuits",
    "opportunity_narratives",
    "funder_candidates",
    "funders",
    "recipient_orgs",
    "grants",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--sqlite",
        default = "./grant_prospector.db",
        help    = "Source SQLite DB path. Default: ./grant_prospector.db",
    )
    parser.add_argument(
        "--postgres",
        default = os.getenv("DATABASE_URL", ""),
        help    = ("Target Postgres URL. Defaults to $DATABASE_URL if set. "
                   "Must start with postgresql:// or postgresql+psycopg://"),
    )
    args = parser.parse_args()

    if not args.postgres:
        log.error("No Postgres URL — pass --postgres or set DATABASE_URL.")
        sys.exit(2)
    if not args.postgres.startswith("postgres"):
        log.error("--postgres must be a postgresql:// URL, got %r", args.postgres)
        sys.exit(2)

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        log.error("SQLite source not found at %s", sqlite_path)
        sys.exit(2)

    src_url = f"sqlite:///{sqlite_path.resolve()}"
    log.info("Source: %s", src_url)
    log.info("Target: %s", args.postgres)

    src_engine = create_engine(
        src_url,
        connect_args = {"check_same_thread": False},
        echo         = False,
    )

    @event.listens_for(src_engine, "connect")
    def _src_pragma(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    dst_engine = create_engine(args.postgres, pool_pre_ping=True, echo=False)

    # Create target schema if it doesn't exist yet. We import the models
    # in dependency order via the same hook portal/main uses on startup.
    log.info("Building target schema on Postgres (create_all)…")
    from database.db import Base
    # Switch the module-level engine just for the create_all call.
    import database.db as db_mod
    original = db_mod.engine
    db_mod.engine = dst_engine
    try:
        from portal.models import (                                # noqa: F401
            organization, org_profile, user, result, learning,
            opportunity, funder_candidate, grant,
        )
        Base.metadata.create_all(bind=dst_engine)
        db_mod.run_lightweight_migrations()
    finally:
        db_mod.engine = original

    src_inspector = inspect(src_engine)
    dst_inspector = inspect(dst_engine)
    src_tables    = set(src_inspector.get_table_names())
    dst_tables    = set(dst_inspector.get_table_names())

    src_session = sessionmaker(bind=src_engine)()
    dst_session = sessionmaker(bind=dst_engine)()

    total_copied = 0
    for table in TABLE_ORDER:
        if table not in src_tables:
            log.info("Source missing table %s — skipping.", table)
            continue
        if table not in dst_tables:
            log.warning("Target missing table %s — schema mismatch?", table)
            continue

        existing = dst_session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
        if existing:
            log.info("Target %s already has %d rows — skipping.", table, existing)
            continue

        rows = list(src_session.execute(text(f"SELECT * FROM {table}")).mappings())
        if not rows:
            log.info("Source %s is empty — nothing to copy.", table)
            continue

        cols = list(rows[0].keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        insert_sql = text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})")

        for row in rows:
            dst_session.execute(insert_sql, dict(row))
        dst_session.commit()
        log.info("Copied %d row(s) into %s.", len(rows), table)
        total_copied += len(rows)

    # Bump Postgres sequences so the next INSERT picks up after the highest copied id.
    for table in TABLE_ORDER:
        if table not in dst_tables:
            continue
        try:
            dst_session.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                f"(SELECT MAX(id) IS NOT NULL FROM {table}))"
            ))
            dst_session.commit()
        except Exception as e:
            log.warning("Could not bump sequence for %s: %s", table, e)

    log.info("Done. Total rows copied: %d.", total_copied)
    src_session.close()
    dst_session.close()


if __name__ == "__main__":
    main()
