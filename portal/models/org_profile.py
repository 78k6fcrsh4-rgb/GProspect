"""
portal/models/org_profile.py
----------------------------
OrgProfileVersion — versioned storage of an Organization's profile payload.

The agent's OrgProfile (agent/profile.py, a Pydantic dataclass) is the runtime
representation of a nonprofit's mission, programs, geography, populations,
budget, and funder preferences. v1 stored this as a JSON file on disk
(profiles/<slug>.json). v2 stores it as versioned rows in this table so:

  - The profile can be edited through the portal without touching the
    filesystem (Phase 1 intake wizard).
  - We never destructively overwrite — every save creates a new version
    and flips is_current on the previous row.
  - Rolling back to a prior version is just flipping is_current.
  - Re-scoring against a historic profile is possible if we ever need it.

The model is named OrgProfileVersion (not OrgProfile) to avoid colliding
with the Pydantic dataclass of the same name in agent/profile.py. Callers
that want the runtime profile call OrgProfile.from_db_payload(...) on the
payload field.

Application-level invariants (NOT enforced by the DB; the migration uses a
unique (org_id, version) constraint and we coordinate is_current in the
seeder + intake code):

  1. version is per-org and monotonically increases. The first save is 1.
  2. At most one row per org has is_current=True. Helpers in this module
     keep that invariant when creating new versions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, JSON, UniqueConstraint
)
from sqlalchemy.orm import relationship

from database.db import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class OrgProfileVersion(Base):
    """
    A single versioned profile payload for an Organization.

    Per-org versions are sequential starting at 1. Use the
    `create_next_version` helper rather than inserting directly so the
    is_current flag stays in sync.
    """

    __tablename__  = "org_profile_versions"
    __table_args__ = (
        UniqueConstraint("org_id", "version", name="uq_org_profile_versions_org_version"),
    )

    # ── Primary key ───────────────────────────────────────────────────────────
    id = Column(Integer, primary_key=True, index=True)

    # ── Tenant scope ──────────────────────────────────────────────────────────
    org_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable = False,
        index    = True,
    )

    # ── Version metadata ──────────────────────────────────────────────────────
    # version is a per-org sequence: 1, 2, 3, ...
    version = Column(Integer, nullable=False)

    # is_current points at the version the agent + UI should use for this org.
    # Indexed for fast lookup of "the active profile for org X."
    is_current = Column(Boolean, default=False, nullable=False, index=True)

    # ── Payload ───────────────────────────────────────────────────────────────
    # The full OrgProfile JSON. Stored as JSON for cross-dialect compatibility
    # (SQLite serializes to TEXT; Postgres uses jsonb-compatible representation
    # when we migrate). Validate by passing through agent.profile.OrgProfile
    # before saving — the column itself is unstructured.
    payload = Column(JSON, nullable=False)

    # ── Audit ─────────────────────────────────────────────────────────────────
    # created_by_user_id is nullable because seeded versions (initial pilot
    # import from the JSON file) aren't attributable to a user.
    created_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable = True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # ── Relationships ─────────────────────────────────────────────────────────
    organization = relationship("Organization", back_populates="profile_versions")
    created_by   = relationship("User", foreign_keys=[created_by_user_id])

    def __repr__(self) -> str:
        return (
            f"OrgProfileVersion(id={self.id}, org_id={self.org_id}, "
            f"version={self.version}, is_current={self.is_current})"
        )

    def to_dict(self, include_payload: bool = True) -> dict:
        """JSON-able summary. Pass include_payload=False to skip the heavy field."""
        out = {
            "id":                 self.id,
            "org_id":             self.org_id,
            "version":            self.version,
            "is_current":         self.is_current,
            "created_by_user_id": self.created_by_user_id,
            "created_at":         self.created_at.isoformat() if self.created_at else None,
        }
        if include_payload:
            out["payload"] = self.payload
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — call these instead of inserting OrgProfileVersion rows directly,
# so the is_current invariant is preserved.
# ─────────────────────────────────────────────────────────────────────────────

def create_next_version(
    db,
    org_id:             int,
    payload:            dict,
    created_by_user_id: int | None = None,
) -> OrgProfileVersion:
    """
    Persist a new profile version for `org_id`, flipping is_current=False on
    the prior current version (if any) and is_current=True on the new row.
    Does NOT commit — callers commit so the operation can be batched into
    a wider transaction (e.g. intake-wizard save).

    Returns the freshly-inserted row.
    """
    # Find prior current version, if any, and demote it.
    prior = (
        db.query(OrgProfileVersion)
          .filter(OrgProfileVersion.org_id == org_id,
                  OrgProfileVersion.is_current == True)  # noqa: E712
          .one_or_none()
    )

    next_version = 1
    if prior is not None:
        prior.is_current = False
        next_version = prior.version + 1
    else:
        # No current row, but there could still be historical versions
        # (e.g. if the prior is_current was manually reset). Be defensive.
        max_existing = (
            db.query(OrgProfileVersion.version)
              .filter(OrgProfileVersion.org_id == org_id)
              .order_by(OrgProfileVersion.version.desc())
              .first()
        )
        if max_existing is not None:
            next_version = max_existing[0] + 1

    new_row = OrgProfileVersion(
        org_id             = org_id,
        version            = next_version,
        is_current         = True,
        payload            = payload,
        created_by_user_id = created_by_user_id,
    )
    db.add(new_row)
    db.flush()
    return new_row


def get_current_for_org(db, org_id: int) -> OrgProfileVersion | None:
    """Returns the active OrgProfileVersion row for the given org, or None."""
    return (
        db.query(OrgProfileVersion)
          .filter(OrgProfileVersion.org_id == org_id,
                  OrgProfileVersion.is_current == True)  # noqa: E712
          .one_or_none()
    )
