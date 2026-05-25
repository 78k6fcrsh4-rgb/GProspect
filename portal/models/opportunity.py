"""
portal/models/opportunity.py
----------------------------
Per-opportunity per-org state introduced in Phase 1b.

Two tables:

  OpportunityPursuit
    Tracks the org's pursuit decision (Pursuing / Watching / Passed) plus
    an audit trail of who set it and when. One row per (org_id, opp_key).
    Inserted lazily — orgs without a row are "New" by default.

  OpportunityNarrative
    Cache for the Claude-generated narrative + scored breakdown. Generated
    on demand when a card is first expanded; cached until the org's profile
    version changes (cache key includes profile_version). Skips a Claude
    call per opportunity per page load.

`opp_key` is a stable SHA-256 hash of (funder_name, program_name) computed
by agent/opportunities.py::compute_opportunity_key. Identical (funder,
program) tuples across runs produce the same key, so pursuit state survives
new agent runs as long as the funder/program strings don't drift.

NOTE: opp_key is NOT an FK to grant_results — the active result set still
lives in CSV files on disk (outputs/<slug>/grant_prospects_*.csv). When
Phase 2 moves results into the DB this can become a proper foreign key.
For now: opportunity rows can be orphaned if the CSV no longer surfaces
the opportunity. The orgs router treats orphans as "unknown" and lets the
user clear them.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Enum, ForeignKey, Integer, JSON, String, Text,
    UniqueConstraint
)
from sqlalchemy.orm import relationship

from database.db import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class PursuitStatus(str, enum.Enum):
    """
    Lifecycle for an opportunity in the org's pipeline.

    PURSUING — actively working on a submission. Counts against capacity,
               surfaces in conflict warnings.
    WATCHING — interesting but not started. Doesn't count against capacity.
    PASSED   — explicitly declined. Hidden from the default Prospects view
               but visible in History / All.
    """
    PURSUING = "pursuing"
    WATCHING = "watching"
    PASSED   = "passed"


class OpportunityPursuit(Base):
    """
    Per-(org, opportunity) pursuit decision + audit fields.

    Application invariant: at most one row per (org_id, opp_key). The
    helper portal.models.opportunity.set_pursuit_status preserves it.
    """

    __tablename__  = "opportunity_pursuits"
    __table_args__ = (
        UniqueConstraint("org_id", "opp_key", name="uq_pursuits_org_opp"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # ── Tenant scope ──────────────────────────────────────────────────────────
    org_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable = False,
        index    = True,
    )

    # ── Opportunity identity ──────────────────────────────────────────────────
    opp_key      = Column(String(64), nullable=False, index=True)
    funder_name  = Column(String, nullable=True)   # denormalized for fast list rendering
    program_name = Column(String, nullable=True)
    deadline     = Column(String, nullable=True)   # ISO date string from the CSV row

    # ── Status ────────────────────────────────────────────────────────────────
    status = Column(
        Enum(PursuitStatus),
        nullable = False,
        default  = PursuitStatus.WATCHING,
    )

    # ── Audit ─────────────────────────────────────────────────────────────────
    updated_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notes      = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=_utcnow,
                        onupdate=_utcnow, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # ── Relationships ─────────────────────────────────────────────────────────
    organization = relationship("Organization")
    updated_by   = relationship("User", foreign_keys=[updated_by_user_id])

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "opp_key":      self.opp_key,
            "funder_name":  self.funder_name,
            "program_name": self.program_name,
            "deadline":     self.deadline,
            "status":       self.status.value if self.status else None,
            "notes":        self.notes,
            "updated_at":   self.updated_at.isoformat() if self.updated_at else None,
            "created_at":   self.created_at.isoformat() if self.created_at else None,
        }


class OpportunityNarrative(Base):
    """
    Cache row for one (org, opportunity, profile_version) tuple.

    Looking up by (org_id, opp_key, profile_version) — if the org saves
    a new profile version, the next narrative call re-generates instead
    of serving stale text. Old rows are kept (cheap insurance, easy to
    purge later).
    """

    __tablename__  = "opportunity_narratives"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "opp_key", "profile_version",
            name = "uq_narratives_org_opp_profver",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    # ── Tenant scope ──────────────────────────────────────────────────────────
    org_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable = False,
        index    = True,
    )

    # ── Cache key ─────────────────────────────────────────────────────────────
    opp_key         = Column(String(64), nullable=False, index=True)
    profile_version = Column(Integer, nullable=False)

    # ── Cached output ─────────────────────────────────────────────────────────
    # conversational_md: 3-5 sentence paragraph rendered on the expanded card.
    # scored_breakdown:  per-dimension scores + one-line reasons, displayed
    #                    inside the "Show details" expander.
    conversational_md = Column(Text, nullable=False)
    scored_breakdown  = Column(JSON, nullable=False, default=dict)

    # ── Audit ─────────────────────────────────────────────────────────────────
    model_used   = Column(String, nullable=True)
    generated_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # ── Relationships ─────────────────────────────────────────────────────────
    organization = relationship("Organization")

    def to_dict(self) -> dict:
        return {
            "id":                self.id,
            "opp_key":           self.opp_key,
            "profile_version":   self.profile_version,
            "conversational_md": self.conversational_md,
            "scored_breakdown":  self.scored_breakdown or {},
            "model_used":        self.model_used,
            "generated_at":      self.generated_at.isoformat() if self.generated_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — call instead of inserting rows directly so invariants hold.
# ─────────────────────────────────────────────────────────────────────────────

def get_pursuit(db, org_id: int, opp_key: str) -> OpportunityPursuit | None:
    """Returns the pursuit row for one (org, opp), or None if unset."""
    return (
        db.query(OpportunityPursuit)
          .filter(OpportunityPursuit.org_id  == org_id,
                  OpportunityPursuit.opp_key == opp_key)
          .one_or_none()
    )


def set_pursuit_status(
    db,
    org_id:             int,
    opp_key:            str,
    status:             PursuitStatus,
    updated_by_user_id: int | None,
    funder_name:        str | None = None,
    program_name:       str | None = None,
    deadline:           str | None = None,
    notes:              str | None = None,
) -> OpportunityPursuit:
    """
    Upsert a pursuit row to the given status. Does NOT commit — caller
    commits so the operation can be batched.
    """
    row = get_pursuit(db, org_id, opp_key)
    if row is None:
        row = OpportunityPursuit(
            org_id      = org_id,
            opp_key     = opp_key,
            funder_name = funder_name,
            program_name= program_name,
            deadline    = deadline,
            status      = status,
            notes       = notes,
            updated_by_user_id = updated_by_user_id,
        )
        db.add(row)
    else:
        row.status             = status
        row.updated_by_user_id = updated_by_user_id
        # Refresh denormalized fields if caller provided them.
        if funder_name  is not None: row.funder_name  = funder_name
        if program_name is not None: row.program_name = program_name
        if deadline     is not None: row.deadline     = deadline
        if notes        is not None: row.notes        = notes
    db.flush()
    return row


def clear_pursuit(db, org_id: int, opp_key: str) -> bool:
    """
    Reset an opportunity back to "New" (no pursuit row). Returns True if
    a row was removed, False if there was nothing to clear. Caller commits.
    """
    row = get_pursuit(db, org_id, opp_key)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def get_narrative(db, org_id: int, opp_key: str,
                  profile_version: int) -> OpportunityNarrative | None:
    """Cache lookup. Returns None on miss."""
    return (
        db.query(OpportunityNarrative)
          .filter(OpportunityNarrative.org_id          == org_id,
                  OpportunityNarrative.opp_key         == opp_key,
                  OpportunityNarrative.profile_version == profile_version)
          .one_or_none()
    )


def save_narrative(
    db,
    org_id:           int,
    opp_key:          str,
    profile_version:  int,
    conversational_md:str,
    scored_breakdown: dict,
    model_used:       str | None = None,
) -> OpportunityNarrative:
    """Insert (or upsert) a narrative cache row. Caller commits."""
    existing = get_narrative(db, org_id, opp_key, profile_version)
    if existing is not None:
        existing.conversational_md = conversational_md
        existing.scored_breakdown  = scored_breakdown
        existing.model_used        = model_used
        existing.generated_at      = _utcnow()
        db.flush()
        return existing

    row = OpportunityNarrative(
        org_id            = org_id,
        opp_key           = opp_key,
        profile_version   = profile_version,
        conversational_md = conversational_md,
        scored_breakdown  = scored_breakdown,
        model_used        = model_used,
    )
    db.add(row)
    db.flush()
    return row
