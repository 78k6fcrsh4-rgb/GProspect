"""
portal/routers/admin.py
-----------------------
Admin-only endpoints for the grant prospecting portal.

All endpoints here require Admin role.
Regular users receive 403 Forbidden.

Endpoints:
    GET  /admin/users              — list all users in org
    PUT  /admin/users/{id}/deactivate — deactivate a user
    GET  /admin/watch-list         — view active watch list
    POST /admin/watch-list         — add source to watch list
    DELETE /admin/watch-list       — remove source from watch list
    GET  /admin/learning-log       — view learning log entries
    GET  /admin/agent-state        — view agent state stats
    GET  /admin/settings           — view current agent settings
    PUT  /admin/settings           — update agent settings
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.profile import OrgProfile
from database.db import get_db
from portal.auth.dependencies import get_current_admin
from portal.models.user import User, UserRole

router = APIRouter(prefix="/admin", tags=["Admin"])


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────

class WatchListSourceRequest(BaseModel):
    """Request body for adding a source to the watch list."""
    name:     str
    url:      str
    priority: str = "medium"
    notes:    Optional[str] = None


class AgentSettingsUpdate(BaseModel):
    """Request body for updating agent settings."""
    exclude_federal:       Optional[bool]  = None
    exclude_state:         Optional[bool]  = None
    deadline_floor_days:   Optional[int]   = None
    deadline_ceiling_days: Optional[int]   = None
    min_composite_score:   Optional[float] = None


class RemoveSourceRequest(BaseModel):
    """Request body for removing a source from the watch list."""
    url: str


# ─────────────────────────────────────────────────────────────────────────────
# User management endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users")
def list_users(
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    """
    Lists all users in the admin's organization.

    Admins can only see users from their own org.

    Args:
        current_admin: Must be Admin (auto-injected).
        db:            Database session.

    Returns:
        List of user dictionaries.
    """
    users = db.query(User).filter(
        User.org_name == current_admin.org_name
    ).all()

    return [u.to_dict() for u in users]


@router.put("/users/{user_id}/deactivate")
def deactivate_user(
    user_id:       int,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    """
    Deactivates a user account.

    Deactivated users cannot log in. Admins cannot
    deactivate their own account.

    Args:
        user_id:       ID of the user to deactivate.
        current_admin: Must be Admin (auto-injected).
        db:            Database session.

    Returns:
        Success message.

    Raises:
        HTTPException 400: Cannot deactivate own account.
        HTTPException 404: User not found.
        HTTPException 403: User belongs to different org.
    """
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"User with ID {user_id} not found.",
        )

    if user.org_name != current_admin.org_name:
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Cannot modify users from a different organization.",
        )

    if user.id == current_admin.id:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = "Administrators cannot deactivate their own account.",
        )

    user.is_active = False
    db.commit()

    return {"message": f"User {user.email} has been deactivated."}


@router.put("/users/{user_id}/activate")
def activate_user(
    user_id:       int,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    """
    Reactivates a previously deactivated user account.

    Args:
        user_id:       ID of the user to activate.
        current_admin: Must be Admin (auto-injected).
        db:            Database session.

    Returns:
        Success message.
    """
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"User with ID {user_id} not found.",
        )

    if user.org_name != current_admin.org_name:
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Cannot modify users from a different organization.",
        )

    user.is_active = True
    db.commit()

    return {"message": f"User {user.email} has been reactivated."}


# ─────────────────────────────────────────────────────────────────────────────
# Watch list endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/watch-list")
def get_watch_list(
    current_admin: User = Depends(get_current_admin),
):
    """
    Returns the agent's current watch list of monitored sources.

    Args:
        current_admin: Must be Admin (auto-injected).

    Returns:
        List of watch list source dictionaries.
    """
    from agent.state import AgentState

    profile = OrgProfile.find_for_org(current_admin.org_name)
    if not profile:
        return []

    state = AgentState(profile)
    return {
        "total_sources":        len(state.get_watch_list()),
        "high_priority":        len(state.get_high_priority_sources()),
        "sources":              state.get_watch_list(),
    }


@router.post("/watch-list")
def add_watch_list_source(
    request:       WatchListSourceRequest,
    current_admin: User = Depends(get_current_admin),
):
    """
    Manually adds a source to the agent's watch list.

    Args:
        request:       Source details to add.
        current_admin: Must be Admin (auto-injected).

    Returns:
        Success or already-exists message.
    """
    from agent.state import AgentState

    profile = OrgProfile.find_for_org(current_admin.org_name)
    if not profile:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "Organization profile not found.",
        )

    state = AgentState(profile)
    added = state.add_to_watch_list(
        name        = request.name,
        url         = request.url,
        source_type = "foundation_website",
        priority    = request.priority,
        added_by    = f"admin:{current_admin.email}",
        notes       = request.notes or "",
    )
    state.save()

    if added:
        return {"message": f"Successfully added '{request.name}' to watch list."}
    else:
        return {"message": f"'{request.name}' is already in the watch list."}


@router.delete("/watch-list")
def remove_watch_list_source(
    request:       RemoveSourceRequest,
    current_admin: User = Depends(get_current_admin),
):
    """
    Removes a source from the agent's watch list by URL.

    Only admins can remove sources — the agent itself
    can only add sources, never remove them.

    Args:
        request:       URL of the source to remove.
        current_admin: Must be Admin (auto-injected).

    Returns:
        Success or not-found message.
    """
    from agent.state import AgentState

    profile = OrgProfile.find_for_org(current_admin.org_name)
    if not profile:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "Organization profile not found.",
        )

    state   = AgentState(profile)
    removed = state.remove_from_watch_list(request.url)
    state.save()

    if removed:
        return {"message": f"Successfully removed {request.url} from watch list."}
    else:
        return {"message": f"URL not found in watch list: {request.url}"}


# ─────────────────────────────────────────────────────────────────────────────
# Learning log endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/learning-log")
def get_learning_log(
    limit:         int  = 20,
    current_admin: User = Depends(get_current_admin),
):
    """
    Returns recent learning log entries showing what the
    agent has changed about itself over time.

    Args:
        limit:         Maximum entries to return.
        current_admin: Must be Admin (auto-injected).

    Returns:
        Dictionary with log stats and recent entries.
    """
    from learning.learning_log import LearningLog

    profile = OrgProfile.find_for_org(current_admin.org_name)
    if not profile:
        return {"entries": [], "stats": {}}

    log     = LearningLog(profile)
    entries = log.get_recent_changes(limit=limit)
    stats   = log.get_stats()

    return {
        "stats":   stats,
        "entries": entries,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Agent state endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/agent-state")
def get_agent_state(
    current_admin: User = Depends(get_current_admin),
):
    """
    Returns the current agent state statistics.

    Shows how many opportunities have been seen, how many
    sources are being monitored, and run history summary.

    Args:
        current_admin: Must be Admin (auto-injected).

    Returns:
        Agent state statistics dictionary.
    """
    from agent.state import AgentState

    profile = OrgProfile.find_for_org(current_admin.org_name)
    if not profile:
        return {}

    state = AgentState(profile)
    stats = state.get_stats()

    return {
        "stats":       stats,
        "run_history": state.get_run_history(limit=5),
    }


@router.get("/settings")
def get_settings(
    current_admin: User = Depends(get_current_admin),
):
    """
    Returns the current agent settings for this organization.

    Args:
        current_admin: Must be Admin (auto-injected).

    Returns:
        Current agent settings dictionary.
    """
    profile = OrgProfile.find_for_org(current_admin.org_name)
    if not profile:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "Organization profile not found.",
        )

    return {
        "org_name":            profile.org_name,
        "exclude_federal":     profile.settings.exclude_federal,
        "exclude_state":       profile.settings.exclude_state,
        "deadline_floor_days": profile.settings.deadline_floor_days,
        "deadline_ceiling_days": profile.settings.deadline_ceiling_days,
        "min_composite_score": profile.settings.min_composite_score,
        "known_funders_count": len(profile.known_funders),
        "program_areas":       [p.value for p in profile.program_areas],
        "geography":           f"{profile.geography.city}, {profile.geography.state}",
        "grant_range":         f"${profile.budget.request_floor:,} – ${profile.budget.request_ceiling:,}",
    }


