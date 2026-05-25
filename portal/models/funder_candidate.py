"""
portal/models/funder_candidate.py
---------------------------------
FunderCandidate — a foundation surfaced by the Phase 2 discovery cycle
as a plausible match for the org's profile.

Schema highlights:
  - One row per (org_id, ein). Unique-constrained.
  - status enum: candidate / watching / engaged / dismissed.
      candidate  — newly discovered, awaiting review.
      watching   — interesting, keep an eye on, future cycles refresh.
      engaged    — actively cultivating; don't surface as a fresh
                   discovery (but keep updating the score + signals).
      dismissed  — not a fit; stop suggesting and don't update.
  - discovered_at + last_seen_at — let the UI distinguish "new today"
    from "still in the candidate pool after N cycles" and lets us
    prune candidates that haven't been seen in N cycles.
  - signals: JSON dict of structured match indicators (state_match,
    ntee_match, asset_size_band, etc.) — drives the rationale text
    and any future re-ranking.

The discovery cycle uses upsert_candidate() so re-running is idempotent:
new discoveries insert, repeat discoveries just refresh score + signals
+ last_seen_at without resetting the user's status choice.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database.db import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class CandidateStatus(str, enum.Enum):
    CANDIDATE = "candidate"
    WATCHING  = "watching"
    ENGAGED   = "engaged"
    DISMISSED = "dismissed"


class FunderCandidate(Base):
    """
    A foundation discovered as a possible match for the org's profile.

    Identity is (org_id, ein) — same foundation across two orgs gets two
    rows so each org's status/notes stay independent.
    """

    __tablename__  = "funder_candidates"
    __table_args__ = (
        UniqueConstraint("org_id", "ein", name="uq_funder_candidates_org_ein"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # ── Tenancy ───────────────────────────────────────────────────────────────
    org_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable = False,
        index    = True,
    )

    # ── Funder identity (denormalized for fast list rendering) ────────────────
    ein            = Column(String(11), nullable=False, index=True)   # "XX-XXXXXXX"
    funder_name    = Column(String,     nullable=False)
    funder_city    = Column(String,     nullable=True)
    funder_state   = Column(String(2),  nullable=True)
    funder_zipcode = Column(String,     nullable=True)
    ntee_code      = Column(String,     nullable=True)
    subseccd       = Column(Integer,    nullable=True)

    # ── Scoring ───────────────────────────────────────────────────────────────
    score     = Column(Float,   nullable=False, default=0.0)
    rationale = Column(Text,    nullable=True)
    signals   = Column(JSON,    nullable=False, default=dict)

    # ── Status ────────────────────────────────────────────────────────────────
    status = Column(
        Enum(CandidateStatus),
        nullable = False,
        default  = CandidateStatus.CANDIDATE,
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    discovered_at      = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen_at       = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    updated_at         = Column(DateTime(timezone=True), default=_utcnow,
                                onupdate=_utcnow, nullable=False)

    # ── Relationships ─────────────────────────────────────────────────────────
    organization = relationship("Organization")
    updated_by   = relationship("User", foreign_keys=[updated_by_user_id])

    def to_dict(self) -> dict:
        return {
            "id":                 self.id,
            "ein":                self.ein,
            "funder_name":        self.funder_name,
            "funder_city":        self.funder_city,
            "funder_state":       self.funder_state,
            "funder_zipcode":     self.funder_zipcode,
            "ntee_code":          self.ntee_code,
            "subseccd":           self.subseccd,
            "score":              self.score,
            "rationale":          self.rationale,
            "signals":            self.signals or {},
            "status":             self.status.value if self.status else None,
            "discovered_at":      self.discovered_at.isoformat() if self.discovered_at else None,
            "last_seen_at":       self.last_seen_at.isoformat()  if self.last_seen_at  else None,
            "updated_at":         self.updated_at.isoformat()    if self.updated_at    else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_candidate(db, org_id: int, ein: str) -> FunderCandidate | None:
    return (
        db.query(FunderCandidate)
          .filter(FunderCandidate.org_id == org_id,
                  FunderCandidate.ein    == ein)
          .one_or_none()
    )


def upsert_candidate(
    db,
    *,
    org_id:      int,
    ein:         str,
    funder_name: str,
    score:       float,
    rationale:   str,
    signals:     dict,
    funder_city:    str | None = None,
    funder_state:   str | None = None,
    funder_zipcode: str | None = None,
    ntee_code:      str | None = None,
    subseccd:       int | None = None,
) -> tuple[FunderCandidate, bool]:
    """
    Idempotent insert-or-refresh. Returns (row, was_inserted).

    For existing rows: refreshes score / rationale / signals /
    last_seen_at and denormalized name+location fields. Does NOT touch
    the user's `status` choice — once a user has marked a candidate
    Watching/Engaged/Dismissed, repeat discovery cycles preserve it.

    Caller commits.
    """
    existing = get_candidate(db, org_id=org_id, ein=ein)
    if existing is None:
        row = FunderCandidate(
            org_id         = org_id,
            ein            = ein,
            funder_name    = funder_name,
            funder_city    = funder_city,
            funder_state   = funder_state,
            funder_zipcode = funder_zipcode,
            ntee_code      = ntee_code,
            subseccd       = subseccd,
            score          = score,
            rationale      = rationale,
            signals        = signals,
            status         = CandidateStatus.CANDIDATE,
        )
        db.add(row)
        db.flush()
        return row, True

    # Refresh — preserve user's status decision.
    existing.funder_name    = funder_name
    if funder_city    is not None: existing.funder_city    = funder_city
    if funder_state   is not None: existing.funder_state   = funder_state
    if funder_zipcode is not None: existing.funder_zipcode = funder_zipcode
    if ntee_code      is not None: existing.ntee_code      = ntee_code
    if subseccd       is not None: existing.subseccd       = subseccd
    existing.score        = score
    existing.rationale    = rationale
    existing.signals      = signals
    existing.last_seen_at = _utcnow()
    db.flush()
    return existing, False


def set_status(
    db,
    *,
    org_id:     int,
    ein:        str,
    status:     CandidateStatus,
    user_id:    int | None = None,
) -> FunderCandidate | None:
    """Update only the status field. Returns the row, or None if not found."""
    row = get_candidate(db, org_id=org_id, ein=ein)
    if row is None:
        return None
    row.status             = status
    row.updated_by_user_id = user_id
    db.flush()
    return row
