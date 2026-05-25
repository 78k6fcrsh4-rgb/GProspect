"""
portal/routers/funders.py
-------------------------
Phase 2 — funder discovery surface.

  GET  /funders/candidates            — list candidates (status-filtered)
  GET  /funders/{ein}                 — detail page: org + 5-year filings
  POST /funders/{ein}/status          — set candidate status
  POST /discovery/run                 — admin-only; runs the cycle in
                                        BackgroundTasks (returns 202)

The detail endpoint reads ProPublica live for the financials + trajectory
chart. We don't persist filings — they're cheap to re-fetch and the
freshness matters. Frontend can memoize the response.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.discovery                 import discover_funders, DiscoveryResult
from agent.peer_match                import (
    find_peer_grants_for_funder,
    warm_path_summary_for_org,
)
from agent.profile                   import OrgProfile
from database.db                     import SessionLocal, get_db
from portal.auth.dependencies        import get_current_admin, get_current_user
from portal.models.funder_candidate  import (
    CandidateStatus,
    FunderCandidate,
    get_candidate,
    set_status,
)
from portal.models.org_profile       import get_current_for_org
from portal.models.user              import User
from tools.propublica                import ProPublicaClient

log = logging.getLogger(__name__)

router        = APIRouter(prefix="/funders",   tags=["Funders"])
discovery_rtr = APIRouter(prefix="/discovery", tags=["Funder Discovery"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class CandidateStatusUpdate(BaseModel):
    status: str   # one of: candidate / watching / engaged / dismissed
    notes:  Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# GET /funders/candidates
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/candidates")
def list_candidates(
    pursuit_status: Optional[str] = None,
    limit:          int  = 200,
    current_user:   User = Depends(get_current_user),
    db:             Session = Depends(get_db),
):
    """
    List the calling org's funder candidates, newest+highest-scored first.

    Args:
        pursuit_status: Filter by status (candidate/watching/engaged/dismissed
                        or 'all'). Default: omit dismissed.
        limit:          Max rows to return.
    """
    q = db.query(FunderCandidate).filter(FunderCandidate.org_id == current_user.org_id)

    if pursuit_status:
        wanted = pursuit_status.lower()
        if wanted != "all":
            try:
                q = q.filter(FunderCandidate.status == CandidateStatus(wanted))
            except ValueError:
                raise HTTPException(
                    status_code = status.HTTP_400_BAD_REQUEST,
                    detail      = f"Invalid status '{pursuit_status}'.",
                )
    else:
        q = q.filter(FunderCandidate.status != CandidateStatus.DISMISSED)

    rows = (
        q.order_by(FunderCandidate.score.desc(),
                   FunderCandidate.discovered_at.desc())
         .limit(limit)
         .all()
    )
    return [r.to_dict() for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# GET /funders/{ein}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{ein}")
def get_funder_detail(
    ein:          str,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Funder detail page payload — combines the local FunderCandidate row
    with a live ProPublica fetch of the org + last 5 years of filings.

    The frontend uses this to render:
      - org card (name, location, NTEE, candidate status)
      - 5-year revenue/expenses/assets trajectory chart
      - recent filings list with PDF links
      - rationale + signals from the local candidate row
    """
    candidate = get_candidate(db, org_id=current_user.org_id, ein=ein)
    if candidate is None:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"No FunderCandidate row for EIN {ein!r} in this org.",
        )

    # Live ProPublica fetch
    detail_payload = _fetch_propublica_detail(ein)

    return {
        "candidate":    candidate.to_dict(),
        "propublica":   detail_payload,
    }


def _fetch_propublica_detail(ein: str) -> dict:
    """
    Pull org + last 5 filings from ProPublica. Returns a JSON-able dict
    designed for the frontend trajectory chart. Returns {'error': ...}
    on failure rather than raising — the candidate detail page still
    renders the local row even if the live fetch fails.
    """
    try:
        client  = ProPublicaClient()
        detail  = client.get_organization(ein)
    except Exception as e:
        log.warning("Could not fetch ProPublica detail for EIN=%s: %s", ein, e)
        return {"error": str(e)}

    # Latest 5 filings (most recent first)
    filings = sorted(
        detail.filings_with_data,
        key     = lambda f: (f.tax_prd or 0),
        reverse = True,
    )[:5]

    return {
        "organization": {
            "ein":        detail.organization.ein,
            "name":       detail.organization.name,
            "address":    detail.organization.address,
            "city":       detail.organization.city,
            "state":      detail.organization.state,
            "zipcode":    detail.organization.zipcode,
            "ntee_code":  detail.organization.ntee_code,
            "subseccd":   detail.organization.subseccd,
            "guidestar_url": detail.organization.guidestar_url,
            "nccs_url":      detail.organization.nccs_url,
        },
        "filings": [
            {
                "tax_prd":      f.tax_prd,
                "tax_prd_yr":   f.tax_prd_yr,
                "formtype":     f.formtype,
                "totrevenue":   f.totrevenue,
                "totfuncexpns": f.totfuncexpns,
                "totassetsend": f.totassetsend,
                "totliabend":   f.totliabend,
                "pdf_url":      f.pdf_url,
                "updated":      f.updated,
            }
            for f in filings
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Warm paths (Phase 3b)
# The /warm-paths/summary route uses a literal first segment; the
# /{ein}/warm-paths route is two-segment with a parameterized first and
# literal second. Neither overlaps with the single-segment /{ein} route
# above, so declaration order is not load-bearing.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/warm-paths/summary")
def get_warm_path_summary(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    For each funder in the caller's active candidate pool, return a
    one-row summary of warm-path activity (count of peer grants given
    + most recent year + total $). Powers the per-row badge on the
    Funders list.

    Empty list when:
      - org has no saved profile (we can't expand peer keywords)
      - org has no active candidates
      - candidates exist but none have been ingested yet
    """
    current_profile = get_current_for_org(db, current_user.org_id)
    if current_profile is None:
        return []

    try:
        profile = OrgProfile.model_validate(current_profile.payload or {})
    except Exception as e:
        log.warning("Profile validation failed for org_id=%s: %s",
                    current_user.org_id, e)
        return []

    eins = [
        c.ein for c in
        db.query(FunderCandidate)
          .filter(FunderCandidate.org_id == current_user.org_id,
                  FunderCandidate.status != CandidateStatus.DISMISSED)
          .all()
    ]
    if not eins:
        return []

    summaries = warm_path_summary_for_org(db, profile, eins)
    # Sort: most peer grants first, then most recent year, then alpha
    summaries.sort(
        key = lambda s: (
            -s.peer_grant_count,
            -(s.most_recent_year or 0),
            s.funder_name or "",
        ),
    )
    return [
        {
            "funder_ein":       s.funder_ein,
            "funder_name":      s.funder_name,
            "peer_grant_count": s.peer_grant_count,
            "most_recent_year": s.most_recent_year,
            "total_amount":     s.total_amount,
        }
        for s in summaries
    ]


@router.get("/{ein}/warm-paths")
def get_warm_paths_for_funder(
    ein:          str,
    limit:        int     = 25,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return the funder's grants to recipients we've identified as peers
    of the caller's org. Each hit includes the recipient identity,
    grant amount/year, and a list of match reasons.

    Used inside the funder detail expander to render the warm-paths
    section.
    """
    candidate = get_candidate(db, org_id=current_user.org_id, ein=ein)
    if candidate is None:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"No FunderCandidate for EIN {ein!r} in this org.",
        )

    current_profile = get_current_for_org(db, current_user.org_id)
    if current_profile is None:
        return {
            "ein":   ein,
            "peer_grants": [],
            "note":  "Save your organization profile before requesting warm paths.",
        }

    try:
        profile = OrgProfile.model_validate(current_profile.payload or {})
    except Exception as e:
        return {
            "ein":   ein,
            "peer_grants": [],
            "note":  f"Profile validation failed: {e}",
        }

    hits = find_peer_grants_for_funder(db, profile, ein, limit=limit)
    return {
        "ein":         ein,
        "funder_name": candidate.funder_name,
        "peer_grants": [
            {
                "grant_id":        h.grant_id,
                "recipient_id":    h.recipient_id,
                "recipient_name":  h.recipient_name,
                "recipient_city":  h.recipient_city,
                "recipient_state": h.recipient_state,
                "recipient_ein":   h.recipient_ein,
                "fiscal_year":     h.fiscal_year,
                "amount":          h.amount,
                "purpose":         h.purpose,
                "score":           h.score,
                "reasons":         h.reasons,
            }
            for h in hits
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /funders/{ein}/status
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{ein}/status")
def update_candidate_status(
    ein:          str,
    body:         CandidateStatusUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    try:
        new_status = CandidateStatus(body.status.lower())
    except ValueError:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = f"Invalid status '{body.status}'. Must be one of: "
                          f"{', '.join(s.value for s in CandidateStatus)}.",
        )

    row = set_status(
        db,
        org_id  = current_user.org_id,
        ein     = ein,
        status  = new_status,
        user_id = current_user.id,
    )
    if row is None:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"No FunderCandidate for EIN {ein!r} in this org.",
        )
    db.commit()
    return row.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# POST /discovery/run — admin-only, BackgroundTasks
# ─────────────────────────────────────────────────────────────────────────────

@discovery_rtr.post("/run", status_code=status.HTTP_202_ACCEPTED)
def trigger_discovery_run(
    background:    BackgroundTasks,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    """
    Dispatch a discovery cycle as a background task.

    Returns 202 immediately. Each cycle takes ~30s (≤75 search results +
    ≤30 detail lookups at <1 req/sec).
    """
    current_profile = get_current_for_org(db, current_admin.org_id)
    if current_profile is None:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = "Save your organization profile in Intake before "
                          "running discovery.",
        )

    # Validate the payload through OrgProfile so the background worker
    # doesn't crash on a malformed persisted profile.
    try:
        profile = OrgProfile.model_validate(current_profile.payload or {})
    except Exception as e:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = f"Persisted profile failed validation: {e}",
        )

    background.add_task(
        _run_discovery_in_background,
        org_id    = current_admin.org_id,
        profile   = profile,
    )

    log.info(
        "Discovery cycle queued for org_id=%s by %s",
        current_admin.org_id, current_admin.email,
    )
    return {
        "status":  "accepted",
        "message": (
            "Discovery cycle queued. Typical run takes 30-60 seconds; "
            "poll GET /funders/candidates for updates."
        ),
    }


def _run_discovery_in_background(org_id: int, profile: OrgProfile) -> None:
    """
    Background worker. Opens its own DB session — the FastAPI-managed
    session passed via Depends has already closed by the time this runs.
    """
    db = SessionLocal()
    try:
        log.info("Discovery: starting for org_id=%s", org_id)
        result: DiscoveryResult = discover_funders(
            db      = db,
            org_id  = org_id,
            profile = profile,
        )
        db.commit()
        log.info(
            "Discovery: org_id=%s — seen=%d inserted=%d refreshed=%d "
            "search_calls=%d detail_calls=%d",
            org_id,
            result.candidates_seen,
            result.candidates_inserted,
            result.candidates_refreshed,
            result.search_calls,
            result.detail_calls,
        )
        for note in result.notes:
            log.info("Discovery: %s", note)
    except Exception:
        log.exception("Discovery cycle failed for org_id=%s", org_id)
        db.rollback()
    finally:
        db.close()
