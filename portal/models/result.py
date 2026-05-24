"""
portal/models/result.py
-----------------------
GrantResult database model — placeholder until full implementation.

NOTE: as of Phase 0 (v2) the active /results/* endpoints still read results
from the filesystem (outputs/<slug>/grant_prospects_*.csv). This model is
the eventual home for results once Phase 1 ports reads into the DB. The
org_id FK is added now so the schema is already correct when that happens.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, String, Text
)
from sqlalchemy.orm import relationship

from database.db import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now — drop-in for the deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc)


class GrantResult(Base):
    __tablename__ = "grant_results"

    id           = Column(Integer, primary_key=True, index=True)

    # ── Tenancy ───────────────────────────────────────────────────────────────
    # org_id is the canonical scope (Phase 0). org_name is kept as a
    # denormalized convenience for paths in the legacy filesystem-driven flow.
    org_id   = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable = False,
        index    = True,
    )
    org_name = Column(String, index=True, nullable=False)

    funder_name  = Column(String, nullable=False)
    program_name = Column(String, nullable=False)
    score_final  = Column(Float, nullable=True)
    deadline     = Column(String, nullable=True)
    award_range  = Column(String, nullable=True)
    next_action  = Column(Text, nullable=True)
    run_date     = Column(DateTime(timezone=True), default=_utcnow)
    raw_data     = Column(Text, nullable=True)

    # ── Relationships ─────────────────────────────────────────────────────────
    organization = relationship("Organization", back_populates="grant_results")
