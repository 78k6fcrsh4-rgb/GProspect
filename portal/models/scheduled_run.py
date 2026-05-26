"""
portal/models/scheduled_run.py
------------------------------
Phase 4b — audit log for orchestrator-fired jobs.

One row per (job, org_id, started_at). Jobs that iterate over multiple
orgs log one row per org so the UI can show a per-tenant view; jobs
that don't iterate (e.g., nightly_health_check) log a single row with
org_id=NULL.

  status: 'started' | 'success' | 'failed'
    'started' is transient — written at job start, updated to
    success/failed when the job returns.

The helpers below are the only API the orchestrator uses; the router
queries directly via the SQLAlchemy session.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Enum, ForeignKey, Integer, String, Text
)
from sqlalchemy.orm import relationship

from database.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, enum.Enum):
    STARTED = "started"
    SUCCESS = "success"
    FAILED  = "failed"


class ScheduledRun(Base):
    __tablename__ = "scheduled_runs"

    id = Column(Integer, primary_key=True, index=True)

    # Identity of the firing job
    job_name = Column(String, nullable=False, index=True)

    # Optional tenant scope — NULL for cross-org jobs like health_check
    org_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable = True,
        index    = True,
    )

    # Timing
    started_at  = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Outcome
    status  = Column(Enum(RunStatus), default=RunStatus.STARTED, nullable=False)
    message = Column(Text, nullable=True)

    organization = relationship("Organization")

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "job_name":    self.job_name,
            "org_id":      self.org_id,
            "started_at":  self.started_at.isoformat()  if self.started_at  else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "status":      self.status.value if self.status else None,
            "message":     self.message,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def log_run_started(db, *, job_name: str, org_id: int | None = None) -> ScheduledRun:
    """Insert a 'started' row. Caller commits."""
    row = ScheduledRun(
        job_name = job_name,
        org_id   = org_id,
        status   = RunStatus.STARTED,
    )
    db.add(row)
    db.flush()
    return row


def log_run_finished(
    db,
    row:     ScheduledRun,
    status:  RunStatus,
    message: str | None = None,
) -> ScheduledRun:
    """
    Update an existing row with finish time + status. Caller commits.

    NOTE: SQLite roundtrips `DateTime(timezone=True)` as naive datetimes
    even though we declared the column tz-aware. The started_at we read
    back via the ORM may therefore be naive while finished_at (set in
    Python) is tz-aware. Coerce both to UTC before subtracting so
    duration_ms arithmetic never crashes.
    """
    row.finished_at = _utcnow()
    row.status      = status
    row.message     = message
    started  = row.started_at
    finished = row.finished_at
    if started is not None and finished is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        delta = finished - started
        row.duration_ms = int(delta.total_seconds() * 1000)
    db.flush()
    return row


def recent_runs(db, *, job_name: str | None = None, limit: int = 50) -> list[ScheduledRun]:
    """Most recent runs first. Filter by job_name if provided."""
    q = db.query(ScheduledRun)
    if job_name:
        q = q.filter(ScheduledRun.job_name == job_name)
    return q.order_by(ScheduledRun.started_at.desc()).limit(limit).all()
