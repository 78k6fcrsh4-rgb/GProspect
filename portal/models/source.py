"""
portal/models/source.py
-----------------------
Phase 5 — source-monitoring data model.

  MonitoredSource: a URL we periodically check for new grant content.
    - org_id is NULLABLE: NULL = global (visible to every org);
      non-null = scoped to one tenant.
    - kind: 'rss' / 'page' / 'custom'.
        rss   — parsed via feedparser-style flow; items are new entries.
        page  — generic HTML fetch + content hash; "found content" = the
                hash changed.
        custom — dispatched to a named parser in tools/scrapers/.
    - parser_key: required when kind=='custom'. Looked up against a
      registry in tools/source_monitor.py.
    - config: free-form JSON for parser overrides (selectors, keywords,
      max_items, etc.). Unused by generic kinds.

  SourceCheck: one row per check attempt. The audit log.

Health is computed on-the-fly in the router from the MonitoredSource
fields (last_success_at, last_failure_at, failure_count). No separate
"health" column — too easy to drift.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing   import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, ForeignKey, Integer, JSON, String, Text
)
from sqlalchemy.orm import relationship

from database.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceKind(str, enum.Enum):
    RSS    = "rss"
    PAGE   = "page"
    CUSTOM = "custom"


class CheckStatus(str, enum.Enum):
    STARTED   = "started"
    SUCCESS   = "success"
    UNCHANGED = "unchanged"   # successful check, no new content
    FAILED    = "failed"


# ─────────────────────────────────────────────────────────────────────────────
# MonitoredSource
# ─────────────────────────────────────────────────────────────────────────────

class MonitoredSource(Base):
    """A URL we monitor for new grant-prospecting content."""

    __tablename__ = "monitored_sources"

    id = Column(Integer, primary_key=True, index=True)

    # Tenancy — nullable for global sources.
    org_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable = True,
        index    = True,
    )

    name        = Column(String, nullable=False)
    url         = Column(String, nullable=False, index=True)
    kind        = Column(Enum(SourceKind), nullable=False, default=SourceKind.PAGE)
    parser_key  = Column(String, nullable=True)   # required when kind=custom
    config      = Column(JSON,   nullable=False, default=dict)

    enabled     = Column(Boolean, nullable=False, default=True)

    # Health bookkeeping (updated by check_source / the audit log helper)
    last_checked_at   = Column(DateTime(timezone=True), nullable=True)
    last_success_at   = Column(DateTime(timezone=True), nullable=True)
    last_failure_at   = Column(DateTime(timezone=True), nullable=True)
    last_content_hash = Column(String, nullable=True)
    last_error        = Column(Text,   nullable=True)
    failure_count     = Column(Integer, nullable=False, default=0)
    success_count     = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow,
                        onupdate=_utcnow, nullable=False)

    organization = relationship("Organization")
    checks       = relationship(
        "SourceCheck",
        back_populates = "source",
        cascade        = "all, delete-orphan",
        order_by       = "desc(SourceCheck.started_at)",
    )

    def to_dict(self, *, include_health: bool = True) -> dict:
        out = {
            "id":         self.id,
            "org_id":     self.org_id,
            "name":       self.name,
            "url":        self.url,
            "kind":       self.kind.value if self.kind else None,
            "parser_key": self.parser_key,
            "config":     self.config or {},
            "enabled":    bool(self.enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_health:
            out.update({
                "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
                "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
                "last_failure_at": self.last_failure_at.isoformat() if self.last_failure_at else None,
                "failure_count":   self.failure_count,
                "success_count":   self.success_count,
                "last_error":      (self.last_error or "")[:200] or None,
                "health":          self.derive_health_label(),
            })
        return out

    def derive_health_label(self) -> str:
        """
        Pure derivation from timestamps + counts. Returns one of:
          'green'      last_success in the last 7 days, no recent failure
          'yellow'     last_success > 7 days OR consecutive failures < 3
          'red'        consecutive failures >= 3, OR never succeeded
          'unknown'    no checks yet
        """
        if self.last_checked_at is None:
            return "unknown"
        if self.last_success_at is None:
            return "red"
        days_since_success = (
            _utcnow() - (
                self.last_success_at.replace(tzinfo=timezone.utc)
                if self.last_success_at.tzinfo is None
                else self.last_success_at
            )
        ).total_seconds() / 86400.0
        if self.failure_count >= 3:
            return "red"
        if days_since_success > 7 or self.failure_count > 0:
            return "yellow"
        return "green"


# ─────────────────────────────────────────────────────────────────────────────
# SourceCheck — audit log
# ─────────────────────────────────────────────────────────────────────────────

class SourceCheck(Base):
    __tablename__ = "source_checks"

    id        = Column(Integer, primary_key=True, index=True)
    source_id = Column(
        Integer,
        ForeignKey("monitored_sources.id", ondelete="CASCADE"),
        nullable = False,
        index    = True,
    )

    started_at  = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)

    status      = Column(Enum(CheckStatus), default=CheckStatus.STARTED, nullable=False)
    items_found = Column(Integer, nullable=False, default=0)
    message     = Column(Text,    nullable=True)
    content_hash= Column(String,  nullable=True)

    source = relationship("MonitoredSource", back_populates="checks")

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "source_id":   self.source_id,
            "started_at":  self.started_at.isoformat()  if self.started_at  else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "status":      self.status.value if self.status else None,
            "items_found": self.items_found,
            "message":     self.message,
            "content_hash":self.content_hash,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def begin_check(db, source: MonitoredSource) -> SourceCheck:
    """Insert a 'started' row. Caller commits."""
    row = SourceCheck(source_id=source.id, status=CheckStatus.STARTED)
    db.add(row)
    db.flush()
    return row


def finish_check(
    db,
    *,
    check:        SourceCheck,
    source:       MonitoredSource,
    status:       CheckStatus,
    items_found:  int  = 0,
    message:      Optional[str] = None,
    content_hash: Optional[str] = None,
) -> SourceCheck:
    """
    Complete a SourceCheck row AND update the parent source's health
    bookkeeping (last_success_at / last_failure_at / counts / hash).
    Caller commits.
    """
    check.finished_at = _utcnow()
    check.status      = status
    check.items_found = items_found
    check.message     = message
    check.content_hash= content_hash

    started  = check.started_at
    finished = check.finished_at
    if started and finished:
        if started.tzinfo  is None: started  = started.replace(tzinfo=timezone.utc)
        if finished.tzinfo is None: finished = finished.replace(tzinfo=timezone.utc)
        check.duration_ms = int((finished - started).total_seconds() * 1000)

    # Update parent source
    source.last_checked_at = check.finished_at
    if status in (CheckStatus.SUCCESS, CheckStatus.UNCHANGED):
        source.last_success_at = check.finished_at
        source.failure_count   = 0
        source.last_error      = None
        source.success_count   = (source.success_count or 0) + 1
        if content_hash is not None:
            source.last_content_hash = content_hash
    elif status == CheckStatus.FAILED:
        source.last_failure_at = check.finished_at
        source.failure_count   = (source.failure_count or 0) + 1
        source.last_error      = (message or "")[:1000] or None

    db.flush()
    return check


def list_sources_for_org(db, org_id: int, include_global: bool = True):
    """
    Return enabled MonitoredSources visible to org_id: their own +
    any global (org_id IS NULL) sources when include_global is True.
    """
    q = db.query(MonitoredSource)
    if include_global:
        q = q.filter(
            (MonitoredSource.org_id == org_id)
            | (MonitoredSource.org_id.is_(None))
        )
    else:
        q = q.filter(MonitoredSource.org_id == org_id)
    return q.order_by(MonitoredSource.name).all()
