"""
portal/routers/grants.py
------------------------
Phase 3a — grant-ingestion control plane.

  POST /grants/ingest   — admin-only; runs the targeted ingestion job
                          as a BackgroundTask, returns 202 Accepted.
  GET  /grants/status   — recent funder ingestion stats for the org's
                          candidate pool (counts + last_ingested_at).

The ingester reads from FunderCandidate (per-org) and writes into the
global Funder/Grant tables. Frontend will use /grants/status to render
a "what's been ingested" panel; Phase 3b adds the warm-path queries.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.grants_ingestion          import ingest_for_org, IngestionResult
from database.db                     import SessionLocal, get_db
from portal.auth.dependencies        import get_current_admin, get_current_user
from portal.models.funder_candidate  import CandidateStatus, FunderCandidate
from portal.models.grant             import Funder, Grant
from portal.models.user              import User
from tools.irs_990                   import IrsForm990Client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/grants", tags=["Grants"])


class IngestRunRequest(BaseModel):
    years_back:  int = 3
    max_per_run: int = 30


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
def trigger_ingest_run(
    background:    BackgroundTasks,
    payload:       IngestRunRequest = IngestRunRequest(),
    current_admin: User             = Depends(get_current_admin),
):
    """
    Queue a 990 ingestion run for the calling admin's org candidates.

    Runs in BackgroundTasks (same pattern as /results/run and
    /discovery/run). The worker opens its own DB session.

    Cost: one IRS S3 round-trip per candidate EIN to locate the latest
    filing + one to download it. At ~0.5s between requests, a max-30
    pool takes ~30 seconds. Bandwidth: a few KB to a few MB per filing.
    """
    background.add_task(
        _ingest_in_background,
        org_id       = current_admin.org_id,
        years_back   = payload.years_back,
        max_per_run  = payload.max_per_run,
    )
    log.info(
        "Grants ingestion queued for org_id=%s by %s",
        current_admin.org_id, current_admin.email,
    )
    return {
        "status":  "accepted",
        "message": (
            "Ingestion queued. Typical run takes 30-90 seconds. "
            "Poll GET /grants/status for progress."
        ),
    }


def _ingest_in_background(org_id: int, years_back: int, max_per_run: int) -> None:
    """Background worker — opens its own DB session."""
    db = SessionLocal()
    try:
        log.info("Grants ingestion: starting for org_id=%s", org_id)
        result: IngestionResult = ingest_for_org(
            db          = db,
            org_id      = org_id,
            years_back  = years_back,
            max_per_run = max_per_run,
        )
        db.commit()
        log.info(
            "Grants ingestion: org_id=%s — funders=%d grants_new=%d "
            "grants_refreshed=%d filings_missing=%d filings_failed=%d",
            org_id,
            result.funders_indexed,
            result.grants_inserted,
            result.grants_refreshed,
            result.filings_missing,
            result.filings_failed,
        )
        for note in result.notes:
            log.info("Grants ingestion: %s", note)
    except Exception:
        log.exception("Grants ingestion failed for org_id=%s", org_id)
        db.rollback()
    finally:
        db.close()


@router.get("/status")
def get_ingest_status(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return ingestion stats for the funders in this org's active candidate
    pool. The frontend renders this as a small table: per-candidate
    ingestion state, including the count of grants persisted and the
    last_ingested_at timestamp.
    """
    candidates = (
        db.query(FunderCandidate)
          .filter(FunderCandidate.org_id == current_user.org_id,
                  FunderCandidate.status != CandidateStatus.DISMISSED)
          .all()
    )
    cand_by_ein  = {c.ein: c for c in candidates}
    eins         = list(cand_by_ein.keys())

    funders = (
        db.query(Funder).filter(Funder.ein.in_(eins)).all()
        if eins else []
    )
    funder_by_ein = {f.ein: f for f in funders}

    out = []
    for ein, cand in cand_by_ein.items():
        funder = funder_by_ein.get(ein)
        out.append({
            "ein":                  ein,
            "funder_name":          cand.funder_name,
            "candidate_score":      cand.score,
            "candidate_status":     cand.status.value if cand.status else None,
            "ingested":             funder is not None,
            "last_ingested_at":     funder.last_ingested_at.isoformat() if funder and funder.last_ingested_at else None,
            "last_990pf_year":      funder.last_990pf_year if funder else None,
            "total_grants_indexed": funder.total_grants_indexed if funder else 0,
            "total_amount_indexed": funder.total_amount_indexed if funder else 0,
        })
    # Order: not-yet-ingested first, then by candidate score desc
    out.sort(key=lambda r: (r["ingested"], -(r["candidate_score"] or 0)))
    return out
