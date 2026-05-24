"""
portal/models/organization.py
-----------------------------
Organization database model — the tenancy root.

Every user, profile, result, and learning entry belongs to exactly one
Organization. The slug is the stable, URL-safe identifier that other systems
(intake forms, API paths if we ever expose them, log lines) reference.
The display_name is what humans see.

This is the multi-tenancy foundation introduced in Phase 0 of GProspect v2.
Before this, the portal was single-tenant and used a denormalized org_name
string on every row — that string remains for backward compatibility during
the migration window and is auto-populated from this row.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Enum, Integer, JSON, String
)
from sqlalchemy.orm import relationship

from database.db import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class OrgStatus(str, enum.Enum):
    """
    Lifecycle state for an Organization tenant.

    ACTIVE   — In normal use. All routes accessible.
    PAUSED   — Onboarded but background jobs (discovery cycles) are off.
               Users can still log in and browse historical results.
    ARCHIVED — Read-only; preserved for audit but no new writes accepted.
    """
    ACTIVE   = "active"
    PAUSED   = "paused"
    ARCHIVED = "archived"


class Organization(Base):
    """
    Tenant root for the multi-tenant GProspect deployment.

    Each row is one nonprofit using the portal. Phase 0 ships with two
    seeded rows — Deborah's Place (Chicago, women's housing) and Found
    Village (Cincinnati, youth welfare) — and the seeder is idempotent
    so subsequent startups don't duplicate them.
    """

    __tablename__ = "organizations"

    # ── Primary key ───────────────────────────────────────────────────────────
    id = Column(Integer, primary_key=True, index=True)

    # ── Identity ──────────────────────────────────────────────────────────────
    # slug is the canonical reference used across the codebase and in any
    # future URL surface (e.g. /orgs/{slug}/profile). Must be URL-safe.
    slug         = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, unique=True, nullable=False)

    # ── Status ────────────────────────────────────────────────────────────────
    status = Column(
        Enum(OrgStatus),
        default  = OrgStatus.ACTIVE,
        nullable = False,
    )

    # ── Org-level config ──────────────────────────────────────────────────────
    # Free-form JSON for per-org settings that don't yet have a dedicated
    # column (future: branding for the digest, time-zone, locale, etc.).
    # Empty dict by default so reads never crash on a None payload.
    settings = Column(JSON, nullable=False, default=dict)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow,
                        onupdate=_utcnow, nullable=False)

    # ── Relationships ─────────────────────────────────────────────────────────
    # back_populates names live on the related models. Each is lazy='select'
    # by default; switch to lazy='dynamic' once any of these collections get
    # large enough that loading all rows in memory becomes a footgun.
    users            = relationship("User",              back_populates="organization")
    profile_versions = relationship("OrgProfileVersion", back_populates="organization",
                                    order_by="desc(OrgProfileVersion.version)")
    grant_results    = relationship("GrantResult",       back_populates="organization")
    learning_entries = relationship("LearningEntry",     back_populates="organization")

    def __repr__(self) -> str:
        return f"Organization(id={self.id}, slug={self.slug!r}, status={self.status.value})"

    def to_dict(self) -> dict:
        """Safe JSON-able representation. No reverse relations included."""
        return {
            "id":           self.id,
            "slug":         self.slug,
            "display_name": self.display_name,
            "status":       self.status.value,
            "settings":     self.settings or {},
            "created_at":   self.created_at.isoformat() if self.created_at else None,
            "updated_at":   self.updated_at.isoformat() if self.updated_at else None,
        }
