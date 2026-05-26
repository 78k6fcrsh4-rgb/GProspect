"""
portal/routers/sources.py
-------------------------
Phase 5 — source-monitoring control plane.

  GET    /sources              — list sources visible to caller (own + global)
  POST   /sources              — admin-only; create a new MonitoredSource
  PUT    /sources/{id}         — admin-only; update name/url/enabled/config
  DELETE /sources/{id}         — admin-only; delete
  POST   /sources/{id}/check   — admin-only; run check_source as BG task
  GET    /sources/{id}/runs    — recent SourceCheck audit rows for this source

Sources are visible to any authenticated user (read-only) within their
org, plus all global sources. Writes are admin-only.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.db                  import SessionLocal, get_db
from portal.auth.dependencies     import get_current_admin, get_current_user
from portal.models.source         import (
    MonitoredSource,
    SourceCheck,
    SourceKind,
    list_sources_for_org,
)
from portal.models.user           import User
from tools.scrapers               import PARSER_REGISTRY
from tools.source_monitor         import check_source

log = logging.getLogger(__name__)
router = APIRouter(prefix="/sources", tags=["Sources"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class SourceCreate(BaseModel):
    name:       str             = Field(..., min_length=2)
    url:        str             = Field(..., min_length=8)
    kind:       str             = Field(..., description="rss / page / custom")
    parser_key: Optional[str]   = None
    config:     dict            = Field(default_factory=dict)
    scope:      str             = Field(
        default     = "org",
        description = "'org' = caller's org only, 'global' = all orgs (admin only)",
    )
    enabled:    bool            = True


class SourceUpdate(BaseModel):
    name:       Optional[str]   = None
    url:        Optional[str]   = None
    kind:       Optional[str]   = None
    parser_key: Optional[str]   = None
    config:     Optional[dict]  = None
    enabled:    Optional[bool]  = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_kind(raw: str) -> SourceKind:
    try:
        return SourceKind(raw.lower())
    except ValueError:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = f"Invalid kind {raw!r}. Must be one of: "
                          f"{', '.join(k.value for k in SourceKind)}",
        )


def _scoped_query(db: Session, current_user: User):
    """Source visibility: own org + global rows."""
    return db.query(MonitoredSource).filter(
        (MonitoredSource.org_id == current_user.org_id)
        | (MonitoredSource.org_id.is_(None))
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /sources
# ─────────────────────────────────────────────────────────────────────────────

@router.get("")
@router.get("/")
def list_sources(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    sources = list_sources_for_org(db, current_user.org_id)
    return [s.to_dict() for s in sources]


# ─────────────────────────────────────────────────────────────────────────────
# POST /sources
# ─────────────────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED)
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_source(
    body:          SourceCreate,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    kind = _parse_kind(body.kind)

    if kind == SourceKind.CUSTOM:
        if not body.parser_key:
            raise HTTPException(
                status_code = status.HTTP_400_BAD_REQUEST,
                detail      = "kind='custom' requires parser_key.",
            )
        if body.parser_key not in PARSER_REGISTRY:
            raise HTTPException(
                status_code = status.HTTP_400_BAD_REQUEST,
                detail      = f"Unknown parser_key {body.parser_key!r}. "
                              f"Available: {', '.join(sorted(PARSER_REGISTRY))}",
            )

    # 'global' scope is admin-only (already gated by get_current_admin, but
    # be explicit about which org_id we persist).
    target_org_id = None if body.scope == "global" else current_admin.org_id

    row = MonitoredSource(
        org_id     = target_org_id,
        name       = body.name,
        url        = body.url,
        kind       = kind,
        parser_key = body.parser_key,
        config     = body.config or {},
        enabled    = body.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log.info("Source %s created by %s (org=%s)", row.id, current_admin.email, target_org_id)
    return row.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# PUT /sources/{id}
# ─────────────────────────────────────────────────────────────────────────────

@router.put("/{source_id}")
def update_source(
    source_id:     int,
    body:          SourceUpdate,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    row = _get_owned_or_global(db, current_admin, source_id)
    # Apply patches
    if body.name       is not None: row.name       = body.name
    if body.url        is not None: row.url        = body.url
    if body.kind       is not None: row.kind       = _parse_kind(body.kind)
    if body.parser_key is not None: row.parser_key = body.parser_key
    if body.config     is not None: row.config     = body.config
    if body.enabled    is not None: row.enabled    = body.enabled
    db.commit()
    db.refresh(row)
    return row.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /sources/{id}
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id:     int,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    row = _get_owned_or_global(db, current_admin, source_id)
    db.delete(row)
    db.commit()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# POST /sources/{id}/check
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{source_id}/check", status_code=status.HTTP_202_ACCEPTED)
def manual_check(
    source_id:     int,
    background:    BackgroundTasks,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    row = _get_owned_or_global(db, current_admin, source_id)
    background.add_task(_check_in_background, row.id)
    return {
        "status":  "accepted",
        "message": f"Check queued for {row.name!r}. Poll GET "
                   f"/sources/{row.id}/runs.",
    }


def _check_in_background(source_id: int) -> None:
    db = SessionLocal()
    try:
        row = db.query(MonitoredSource).filter_by(id=source_id).one_or_none()
        if row is None:
            return
        try:
            check_source(db, row)
            db.commit()
        except Exception:
            log.exception("Manual source check failed for source_id=%s", source_id)
            db.rollback()
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# GET /sources/{id}/runs
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{source_id}/runs")
def list_runs(
    source_id:    int,
    limit:        int     = 25,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    _get_owned_or_global(db, current_user, source_id)
    rows = (
        db.query(SourceCheck)
          .filter(SourceCheck.source_id == source_id)
          .order_by(SourceCheck.started_at.desc())
          .limit(limit)
          .all()
    )
    return [r.to_dict() for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Common access helper — enforces "own or global" visibility
# ─────────────────────────────────────────────────────────────────────────────

def _get_owned_or_global(db: Session, user: User, source_id: int) -> MonitoredSource:
    row = (
        db.query(MonitoredSource)
          .filter(MonitoredSource.id == source_id)
          .one_or_none()
    )
    if row is None:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "Source not found.",
        )
    if row.org_id not in (None, user.org_id):
        # 403 (not 404) — prefer "you can see this exists but can't touch it"
        # over leaking other-org IDs via 404 vs 403 distinction.
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Source belongs to a different organization.",
        )
    return row
