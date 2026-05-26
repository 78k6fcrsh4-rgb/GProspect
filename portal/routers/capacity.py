"""
portal/routers/capacity.py
--------------------------
Phase 4a — capacity endpoints.

  GET /orgs/me/capacity        — returns the org's capacity row (or
                                 default if none saved yet)
  PUT /orgs/me/capacity        — admin-only; upsert
  GET /opportunities/capacity-summary
                               — aggregate for the Pipeline meter

The /opportunities list itself gains a `capacity_fit` field per row,
wired in portal/routers/opportunities.py — this router only owns the
capacity model + the dedicated capacity routes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agent.capacity_rank          import summarize_capacity
from database.db                  import get_db
from portal.auth.dependencies     import get_current_admin, get_current_user
from portal.models.capacity       import (
    OrgCapacity,
    get_or_default,
    upsert,
)
from portal.models.opportunity    import OpportunityPursuit, PursuitStatus
from portal.models.user           import User

log = logging.getLogger(__name__)


capacity_rtr = APIRouter(prefix="/orgs/me/capacity", tags=["Capacity"])
opps_extra   = APIRouter(prefix="/opportunities",   tags=["Opportunities"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class AvailabilityWindow(BaseModel):
    start: str = Field(..., description="ISO YYYY-MM-DD")
    end:   str = Field(..., description="ISO YYYY-MM-DD")
    label: str = Field(default="Closed window")


class CapacityUpdate(BaseModel):
    active_pursuits_target: int                       = Field(..., ge=1, le=100)
    availability_windows:   list[AvailabilityWindow]  = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# GET /orgs/me/capacity
# ─────────────────────────────────────────────────────────────────────────────

@capacity_rtr.get("")
@capacity_rtr.get("/")
def get_capacity(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Returns the caller's org capacity row, inserting a default if missing."""
    row = get_or_default(db, current_user.org_id)
    db.commit()
    return row.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# PUT /orgs/me/capacity
# ─────────────────────────────────────────────────────────────────────────────

@capacity_rtr.put("")
@capacity_rtr.put("/")
def update_capacity(
    body:          CapacityUpdate,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    """Admin-only. Idempotent upsert."""
    row = upsert(
        db,
        org_id                 = current_admin.org_id,
        active_pursuits_target = body.active_pursuits_target,
        availability_windows   = [w.model_dump() for w in body.availability_windows],
        user_id                = current_admin.id,
    )
    db.commit()
    log.info(
        "Capacity updated for org_id=%s by %s (target=%d, windows=%d)",
        current_admin.org_id, current_admin.email,
        row.active_pursuits_target, len(row.availability_windows or []),
    )
    return row.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# GET /opportunities/capacity-summary
# ─────────────────────────────────────────────────────────────────────────────

@opps_extra.get("/capacity-summary")
def get_capacity_summary(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Aggregate for the Pipeline meter:
      - active_pursuits_target
      - current_pursuing (how many Pursuit rows in 'pursuing' state)
      - headroom + utilization_pct
      - any availability windows containing today
      - the next future window, if any
    """
    cap = get_or_default(db, current_user.org_id)
    db.commit()

    current_pursuing = (
        db.query(OpportunityPursuit)
          .filter(OpportunityPursuit.org_id == current_user.org_id,
                  OpportunityPursuit.status == PursuitStatus.PURSUING)
          .count()
    )

    summary = summarize_capacity(
        capacity_target      = cap.active_pursuits_target,
        current_pursuing     = current_pursuing,
        availability_windows = cap.availability_windows or [],
    )
    return {
        "active_pursuits_target": summary.active_pursuits_target,
        "current_pursuing":       summary.current_pursuing,
        "headroom":               summary.headroom,
        "utilization_pct":        summary.utilization_pct,
        "closed_windows_active":  summary.closed_windows_active,
        "next_closed_window":     summary.next_closed_window,
    }
