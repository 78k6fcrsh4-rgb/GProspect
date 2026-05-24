"""
portal/routers/orgs.py
----------------------
Organization endpoints — what the caller's tenant looks like.

Phase 0 ships exactly one endpoint:

    GET /orgs/me  — returns the calling user's Organization row.

Phase 1 will add the profile-version endpoints (GET current, POST new
version, GET history) once the intake wizard is built.

Cross-org access is impossible here by construction: every endpoint reads
the caller's org via `current_user.org_id`, never accepts an arbitrary
org_id in the URL or body.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database.db import get_db
from portal.auth.dependencies   import get_current_user
from portal.models.organization import Organization
from portal.models.user         import User

router = APIRouter(prefix="/orgs", tags=["Organizations"])


@router.get("/me")
def get_my_org(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Returns the Organization row the calling user belongs to.

    Used by the frontend to show the org name in the sidebar, drive
    the org-aware empty states, and bootstrap any org-scoped UX.

    Args:
        current_user: Authenticated user (auto-injected).
        db:           Database session.

    Returns:
        Dict with id, slug, display_name, status, settings, timestamps.

    Raises:
        HTTPException 404: The user references an org_id that doesn't exist.
                           Indicates corrupted data — should never happen in
                           normal operation; returning 404 (not 500) lets
                           the frontend surface a clean error.
    """
    org = db.query(Organization).filter(Organization.id == current_user.org_id).one_or_none()
    if org is None:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "Organization not found for the current user.",
        )
    return org.to_dict()
