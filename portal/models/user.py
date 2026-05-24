"""
portal/models/user.py
---------------------
User database model for the grant prospecting portal.

Defines the users table with role-based access control.
Two roles supported:
    admin — full access to all portal capabilities
    user  — read and export access to results only

Each user belongs to one organization. Users from different
organizations cannot see each other's data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, String, Enum
)
import enum

from database.db import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now — drop-in for the deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc)


class UserRole(str, enum.Enum):
    """
    User role enumeration.
    Controls what each user can see and do in the portal.
    """
    ADMIN = "admin"
    USER  = "user"


class User(Base):
    """
    User account database model.

    Each user belongs to one organization (org_name).
    Users can only see data belonging to their organization.
    """

    __tablename__ = "users"

    # ── Primary key ───────────────────────────────────────────────────────────
    id = Column(Integer, primary_key=True, index=True)

    # ── Identity ──────────────────────────────────────────────────────────────
    email       = Column(String, unique=True, index=True, nullable=False)
    full_name   = Column(String, nullable=False)
    org_name    = Column(String, nullable=False, index=True)

    # ── Authentication ────────────────────────────────────────────────────────
    hashed_password = Column(String, nullable=False)

    # ── Role ──────────────────────────────────────────────────────────────────
    role = Column(
        Enum(UserRole),
        default  = UserRole.USER,
        nullable = False
    )

    # ── Status ────────────────────────────────────────────────────────────────
    is_active   = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)

    # ── Token versioning (logout / revoke-all) ────────────────────────────────
    # Every JWT issued for this user encodes the current token_version as the
    # `tv` claim. /auth/logout increments this column, which immediately
    # invalidates every outstanding token for the user — closing the gap that
    # stateless JWTs would otherwise leave open.
    token_version = Column(Integer, default=0, nullable=False)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at  = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at  = Column(DateTime(timezone=True), default=_utcnow,
                        onupdate=_utcnow, nullable=False)
    last_login  = Column(DateTime(timezone=True), nullable=True)

    # ── Password reset ────────────────────────────────────────────────────────
    reset_token            = Column(String, nullable=True)
    reset_token_expires_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"User(id={self.id}, email={self.email}, "
            f"role={self.role}, org={self.org_name})"
        )

    @property
    def is_admin(self) -> bool:
        """Returns True if this user has the Admin role."""
        return self.role == UserRole.ADMIN

    def to_dict(self) -> dict:
        """
        Returns a safe dictionary representation of the user.
        Never includes the hashed password.
        """
        return {
            "id":           self.id,
            "email":        self.email,
            "full_name":    self.full_name,
            "org_name":     self.org_name,
            "role":         self.role.value,
            "is_active":    self.is_active,
            "is_verified":  self.is_verified,
            "created_at":   self.created_at.isoformat() if self.created_at else None,
            "last_login":   self.last_login.isoformat() if self.last_login else None,
        }