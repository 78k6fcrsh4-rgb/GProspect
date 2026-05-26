"""
portal/routers/opportunities.py
-------------------------------
Phase 1b — the per-opportunity working surface.

  GET  /opportunities                       — enriched ranked list
  POST /opportunities/{key}/pursue          — mark Pursuing
  POST /opportunities/{key}/watch           — mark Watching
  POST /opportunities/{key}/pass            — mark Passed
  POST /opportunities/{key}/clear           — clear pursuit state
  POST /opportunities/{key}/narrative       — generate + cache narrative

The underlying CSV result store hasn't moved yet (still
outputs/<slug>/grant_prospects_*.csv). This router enriches CSV rows
with:
  - opp_key            (stable hash; pursuit/narrative join key)
  - deadline_bucket    (hot/warm/cold/past/unknown)
  - pursuit            (per-org pursuit state or None)
  - has_narrative      (bool — cache hit on current profile_version)

The pre-existing /results router is unchanged so the v1-style table view
keeps working; the v2 frontend will read from /opportunities for the
card-based view.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.capacity_rank  import CapacityFit, assess_opportunity
from agent.narrative      import generate_narrative
from agent.opportunities  import (
    classify_deadline,
    compute_opportunity_key,
    parse_days_remaining,
)
from database.db          import get_db
from portal.auth.dependencies      import get_current_user
from portal.models.capacity        import get_or_default as get_capacity_or_default
from portal.models.opportunity     import (
    OpportunityPursuit,
    PursuitStatus,
    clear_pursuit,
    get_narrative,
    get_pursuit,
    save_narrative,
    set_pursuit_status,
)
from portal.models.org_profile     import get_current_for_org
from portal.models.user            import User
from portal.routers.results        import _load_latest_results

log = logging.getLogger(__name__)
router = APIRouter(prefix="/opportunities", tags=["Opportunities"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class OpportunityListItem(BaseModel):
    """One enriched row in the prospect list."""
    rank:                 int
    opp_key:              str
    funder_name:          str
    program_name:         str
    score_final:          Optional[float] = None
    application_deadline: Optional[str]   = None
    days_remaining:       Optional[int]   = None
    deadline_bucket:      str
    award_range:          Optional[str]   = None
    next_action:          Optional[str]   = None
    application_url:      Optional[str]   = None
    source:               Optional[str]   = None
    date_found:           Optional[str]   = None
    pursuit:              Optional[dict]  = None   # OpportunityPursuit.to_dict() or None
    has_narrative:        bool
    capacity_fit:         Optional[dict]  = None   # CapacityFit.to_dict() or None


class NarrativeResponse(BaseModel):
    opp_key:           str
    conversational_md: str
    scored_breakdown:  dict[str, Any]
    cached:            bool
    profile_version:   int
    model_used:        Optional[str] = None


class PursuitNotes(BaseModel):
    """Optional notes that can ride along with a status change."""
    notes: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# GET /opportunities — enriched list
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[OpportunityListItem])
@router.get("/", response_model=list[OpportunityListItem])
def list_opportunities(
    limit:        int  = 200,
    min_score:    Optional[float] = None,
    pursuit:      Optional[str]   = None,   # 'pursuing'|'watching'|'passed'|'new'|'any'
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Returns the enriched ranked prospect list for the calling user's org.

    - Reads the latest grant_prospects CSV from outputs/<slug>/.
    - Computes opp_key + deadline_bucket per row.
    - Joins with the org's pursuit rows so the frontend can render the
      Pursuing/Watching/Passed badge inline.
    - Flags has_narrative=true for rows that have a cached narrative on
      the current profile version (so the frontend knows whether expanding
      will trigger a Claude call).

    Args:
        limit:       Maximum rows to return (default 200).
        min_score:   Optional floor on score_final.
        pursuit:     Filter by pursuit status. 'new' = no pursuit row.
                     'any' = no filter.

    Returns: list of OpportunityListItem.
    """
    raw = _load_latest_results(current_user.org_name)
    if not raw:
        return []

    # Look up current profile_version once for the has_narrative join.
    current_profile = get_current_for_org(db, current_user.org_id)
    profile_version = current_profile.version if current_profile else 0

    # Capacity context, computed once per request.
    capacity         = get_capacity_or_default(db, current_user.org_id)
    db.commit()  # persist the default row if one was just inserted
    current_pursuing = (
        db.query(OpportunityPursuit)
          .filter(OpportunityPursuit.org_id == current_user.org_id,
                  OpportunityPursuit.status == PursuitStatus.PURSUING)
          .count()
    )
    capacity_target  = capacity.active_pursuits_target
    avail_windows    = capacity.availability_windows or []

    out: list[OpportunityListItem] = []
    for idx, row in enumerate(raw):
        score = _safe_float(row.get("score_final"))
        if min_score is not None and (score is None or score < min_score):
            continue

        funder  = (row.get("funder_name")  or "").strip()
        program = (row.get("program_name") or "").strip()
        if not funder and not program:
            continue   # skip mangled rows

        opp_key = compute_opportunity_key(funder, program)
        days    = parse_days_remaining(row.get("days_remaining"))
        bucket  = classify_deadline(days)

        pursuit_row = get_pursuit(db, current_user.org_id, opp_key)
        pursuit_dict = pursuit_row.to_dict() if pursuit_row else None

        # Pursuit filter
        if pursuit:
            wanted = pursuit.lower()
            if wanted == "new":
                if pursuit_row is not None:
                    continue
            elif wanted in {"pursuing", "watching", "passed"}:
                if pursuit_row is None or pursuit_row.status.value != wanted:
                    continue
            # 'any' or anything else: no filter

        narrative_row = get_narrative(db, current_user.org_id, opp_key, profile_version)
        has_narrative = narrative_row is not None

        # Build the opportunity dict in the shape assess_opportunity expects,
        # then merge the resulting CapacityFit into the response.
        provisional_dict = {
            "application_deadline": row.get("application_deadline") or None,
            "pursuit":              pursuit_dict,
        }
        fit = assess_opportunity(
            opportunity          = provisional_dict,
            capacity_target      = capacity_target,
            current_pursuing     = current_pursuing,
            availability_windows = avail_windows,
        )

        # Re-rank by adding the capacity adjustment to score_final, when
        # the row has a base score to adjust.
        ranked_score = score
        if score is not None and fit.score_adjustment:
            ranked_score = score + fit.score_adjustment

        out.append(OpportunityListItem(
            rank                 = idx + 1,
            opp_key              = opp_key,
            funder_name          = funder,
            program_name         = program,
            score_final          = ranked_score,
            application_deadline = row.get("application_deadline") or None,
            days_remaining       = days,
            deadline_bucket      = bucket,
            award_range          = row.get("award_range") or None,
            next_action          = row.get("next_action") or None,
            application_url      = row.get("application_url") or None,
            source               = row.get("source") or None,
            date_found           = row.get("date_found") or None,
            pursuit              = pursuit_dict,
            has_narrative        = has_narrative,
            capacity_fit         = fit.to_dict(),
        ))

        if len(out) >= limit:
            break

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Pursuit state endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{opp_key}/pursue", response_model=dict)
def mark_pursuing(opp_key: str, payload: PursuitNotes = PursuitNotes(),
                  current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    return _change_status(db, current_user, opp_key, PursuitStatus.PURSUING, payload.notes)


@router.post("/{opp_key}/watch", response_model=dict)
def mark_watching(opp_key: str, payload: PursuitNotes = PursuitNotes(),
                  current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    return _change_status(db, current_user, opp_key, PursuitStatus.WATCHING, payload.notes)


@router.post("/{opp_key}/pass", response_model=dict)
def mark_passed(opp_key: str, payload: PursuitNotes = PursuitNotes(),
                current_user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    return _change_status(db, current_user, opp_key, PursuitStatus.PASSED, payload.notes)


@router.post("/{opp_key}/clear", response_model=dict)
def clear_status(opp_key: str,
                 current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    removed = clear_pursuit(db, current_user.org_id, opp_key)
    db.commit()
    return {"opp_key": opp_key, "cleared": removed}


def _change_status(
    db: Session,
    user: User,
    opp_key: str,
    new_status: PursuitStatus,
    notes: Optional[str],
) -> dict:
    """Shared implementation: locates row in latest CSV to denormalize fields, then upserts."""
    funder, program, deadline = _lookup_opp_meta(user.org_name, opp_key)
    row = set_pursuit_status(
        db,
        org_id             = user.org_id,
        opp_key            = opp_key,
        status             = new_status,
        updated_by_user_id = user.id,
        funder_name        = funder,
        program_name       = program,
        deadline           = deadline,
        notes              = notes,
    )
    db.commit()
    return row.to_dict()


def _lookup_opp_meta(org_name: str, opp_key: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Scan the latest CSV for the row matching opp_key. Returns
    (funder_name, program_name, application_deadline) or (None, None, None)
    if the opportunity is no longer in the CSV. Pursuit can still be set
    on an unknown opp_key — useful when a user is editing state on an
    opportunity that's since fallen off the list.
    """
    rows = _load_latest_results(org_name) or []
    for r in rows:
        funder  = (r.get("funder_name")  or "").strip()
        program = (r.get("program_name") or "").strip()
        if compute_opportunity_key(funder, program) == opp_key:
            return funder, program, r.get("application_deadline")
    return None, None, None


# ─────────────────────────────────────────────────────────────────────────────
# Narrative generation
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{opp_key}/narrative", response_model=NarrativeResponse)
def get_or_generate_narrative(
    opp_key:      str,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return the cached narrative for this opportunity at the org's CURRENT
    profile version. If not cached, generate it via Claude, persist, and
    return.

    Caching key: (org_id, opp_key, profile_version). When the org saves a
    new profile version, the next call regenerates instead of serving stale.

    Cost note: each cache miss is one Claude call. With ~30 opportunities
    per org and lazy generation on card expand, the typical session does
    far fewer calls than the worst case. Costs are bounded.
    """
    current_profile = get_current_for_org(db, current_user.org_id)
    if current_profile is None:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = "No profile saved yet for this organization. "
                          "Save one via the Intake wizard before requesting "
                          "narratives.",
        )

    cached = get_narrative(db, current_user.org_id, opp_key, current_profile.version)
    if cached is not None:
        return NarrativeResponse(
            opp_key           = opp_key,
            conversational_md = cached.conversational_md,
            scored_breakdown  = cached.scored_breakdown or {},
            cached            = True,
            profile_version   = cached.profile_version,
            model_used        = cached.model_used,
        )

    # Miss — locate the opportunity in the latest CSV, generate, persist.
    rows = _load_latest_results(current_user.org_name) or []
    target_row = None
    for r in rows:
        funder  = (r.get("funder_name")  or "").strip()
        program = (r.get("program_name") or "").strip()
        if compute_opportunity_key(funder, program) == opp_key:
            target_row = r
            break

    if target_row is None:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "Opportunity not found in the current results set. "
                          "It may have fallen off after a more recent agent run.",
        )

    result = generate_narrative(
        profile_payload = current_profile.payload or {},
        opportunity_row = target_row,
    )

    save_narrative(
        db,
        org_id            = current_user.org_id,
        opp_key           = opp_key,
        profile_version   = current_profile.version,
        conversational_md = result.conversational_md,
        scored_breakdown  = result.scored_breakdown,
        model_used        = result.model_used,
    )
    db.commit()

    return NarrativeResponse(
        opp_key           = opp_key,
        conversational_md = result.conversational_md,
        scored_breakdown  = result.scored_breakdown,
        cached            = False,
        profile_version   = current_profile.version,
        model_used        = result.model_used,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    try:
        if val is None or val == "" or val == "Not scored":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None
