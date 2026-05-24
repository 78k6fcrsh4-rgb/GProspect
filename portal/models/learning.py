"""
portal/models/learning.py
--------------------------
LearningEntry database model — placeholder until full implementation.

As of Phase 0 (v2) the active learning loop still persists state to the
filesystem (outputs/<slug>/learning_log/*.json). This row is the eventual
DB home for those entries; the org_id FK is added now so the schema is
already correct when Phase 1/2 ports reads + writes into the DB.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text
)
from sqlalchemy.orm import relationship

from database.db import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now — drop-in for the deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc)


class LearningEntry(Base):
    __tablename__ = "learning_entries"

    id           = Column(Integer, primary_key=True, index=True)

    # ── Tenancy ───────────────────────────────────────────────────────────────
    org_id   = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable = False,
        index    = True,
    )
    org_name = Column(String, index=True, nullable=False)

    entry_type   = Column(String, nullable=False)
    description  = Column(Text, nullable=True)
    triggered_by = Column(String, nullable=True)
    created_at   = Column(DateTime(timezone=True), default=_utcnow)

    # ── Relationships ─────────────────────────────────────────────────────────
    organization = relationship("Organization", back_populates="learning_entries")
