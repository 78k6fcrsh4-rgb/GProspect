"""
portal/models/capacity.py
-------------------------
OrgCapacity — operational metadata about how much pursuit work an org can
realistically take on, separate from the (mission-stable) OrgProfile.

Schema:
  - org_id (unique): one row per org. Created on first read with defaults.
  - active_pursuits_target: int >= 1. The "we can run N proposals at a
    time" number. The Pipeline view shows N as the budget meter; the
    re-ranking layer demotes prospects when the user is already at/over.
  - availability_windows: JSON list of {start, end, label} blocks.
    Dates are ISO YYYY-MM-DD strings. Treated as INCLUSIVE on both ends.
    Opportunities with deadlines inside any window get demoted + a
    warning attached.
  - updated_by_user_id + updated_at: audit fields.

Helpers:
  - get_or_default(db, org_id): always returns an OrgCapacity object,
    inserting a default row if none exists. Caller commits.
  - upsert(...): idempotent in-place update of the single row.
  - is_within_window(date, windows): pure check, returns (bool, label).

Capacity is intentionally NOT versioned. It's tactical, not strategic —
a user editing "we have bandwidth for 4 not 5" doesn't need a permanent
diff history. The audit fields show who last touched it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, JSON, UniqueConstraint
)
from sqlalchemy.orm import relationship

from database.db import Base


DEFAULT_TARGET = 5   # most pilot CBOs can run ~5 concurrent proposals


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrgCapacity(Base):
    """One row per org. Defaults inserted on first read."""

    __tablename__  = "org_capacity"
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_org_capacity_org"),
    )

    id     = Column(Integer, primary_key=True, index=True)
    org_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable = False,
        index    = True,
    )

    active_pursuits_target = Column(Integer, nullable=False, default=DEFAULT_TARGET)
    availability_windows   = Column(JSON,    nullable=False, default=list)

    updated_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow,
                        onupdate=_utcnow, nullable=False)

    organization = relationship("Organization")
    updated_by   = relationship("User", foreign_keys=[updated_by_user_id])

    def to_dict(self) -> dict:
        return {
            "id":                     self.id,
            "org_id":                 self.org_id,
            "active_pursuits_target": self.active_pursuits_target,
            "availability_windows":   self.availability_windows or [],
            "updated_by_user_id":     self.updated_by_user_id,
            "created_at":             self.created_at.isoformat() if self.created_at else None,
            "updated_at":             self.updated_at.isoformat() if self.updated_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_or_default(db, org_id: int) -> OrgCapacity:
    """
    Return the org's capacity row. If none exists, insert one with default
    values and return it. Caller commits.
    """
    row = (
        db.query(OrgCapacity).filter(OrgCapacity.org_id == org_id).one_or_none()
    )
    if row is not None:
        return row
    row = OrgCapacity(
        org_id                 = org_id,
        active_pursuits_target = DEFAULT_TARGET,
        availability_windows   = [],
    )
    db.add(row)
    db.flush()
    return row


def upsert(
    db,
    *,
    org_id:                 int,
    active_pursuits_target: int,
    availability_windows:   list[dict],
    user_id:                int | None = None,
) -> OrgCapacity:
    """
    Idempotent update. Caller commits. Validates the windows shape but
    does not re-validate dates beyond the YYYY-MM-DD format (cheap; the
    re-ranking layer is forgiving on malformed dates).
    """
    validated_windows = _validate_windows(availability_windows)
    row = get_or_default(db, org_id)
    row.active_pursuits_target = max(1, int(active_pursuits_target or DEFAULT_TARGET))
    row.availability_windows   = validated_windows
    row.updated_by_user_id     = user_id
    db.flush()
    return row


def _validate_windows(raw: list[dict] | None) -> list[dict]:
    """
    Strip rows with missing start/end, normalize to a list of
    {start, end, label} dicts. Bad rows are dropped silently — better
    UX than rejecting the whole save.
    """
    out: list[dict] = []
    for win in raw or []:
        if not isinstance(win, dict):
            continue
        start = (win.get("start") or "").strip()
        end   = (win.get("end")   or "").strip()
        label = (win.get("label") or "").strip()
        if not start or not end:
            continue
        out.append({"start": start, "end": end, "label": label or "Closed window"})
    return out


def is_within_window(d: date | str | None,
                      windows: list[dict] | None) -> tuple[bool, Optional[str]]:
    """
    Return (True, label) if `d` falls inside any availability window,
    else (False, None). Handles ISO date strings, datetimes, and date
    objects. Treats malformed dates as "not in any window" rather than
    raising so callers can pass deadline strings directly.

    Boundaries are inclusive on both ends.
    """
    if d is None or not windows:
        return False, None

    target = _coerce_date(d)
    if target is None:
        return False, None

    for win in windows:
        start = _coerce_date(win.get("start"))
        end   = _coerce_date(win.get("end"))
        if start is None or end is None:
            continue
        if start <= target <= end:
            return True, win.get("label") or "Closed window"
    return False, None


def _coerce_date(value) -> date | None:
    """Best-effort date coercion. Returns None for unparseable input."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Try ISO first; fall back to YYYY-MM-DD if there's a time component.
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None
