"""
portal/routers/results.py
-------------------------
Results endpoints — viewing and exporting grant prospects.

Endpoints:
    GET  /results              — get ranked prospect list
    GET  /results/{id}         — get single result detail
    GET  /results/export/csv   — download CSV export
    GET  /results/export/excel — download Excel export
    GET  /results/runs         — get run history
    POST /results/run          — trigger a new prospecting run

Both Admin and User roles can access all endpoints here.
Results are filtered to the current user's organization only.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.db import get_db
from portal.auth.dependencies import get_current_user, get_current_admin
from portal.models.user import User
from agent.profile import OrgProfile
from agent.state import AgentState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/results", tags=["Results"])


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────

class GrantResultResponse(BaseModel):
    """Single grant result for API response."""
    rank:                     int
    funder_name:              str
    program_name:             str
    score_final:              Optional[float]
    score_composite:          Optional[float]
    application_deadline:     Optional[str]
    days_remaining:           Optional[int]
    award_range:              Optional[str]
    next_action:              Optional[str]
    is_prior_funder:          Optional[str]
    geographic_focus:         Optional[str]
    eligibility_requirements: Optional[str]
    application_url:          Optional[str]
    score_geographic:         Optional[float]
    score_population:         Optional[float]
    score_budget:             Optional[float]
    score_timeline:           Optional[float]
    reason_geographic:        Optional[str]
    reason_population:        Optional[str]
    reason_budget:            Optional[str]
    reason_timeline:          Optional[str]
    source:                   Optional[str]
    date_found:               Optional[str]


class RunSummaryResponse(BaseModel):
    """Summary of a single prospecting run."""
    run_date:       str
    total_results:  int
    top_score:      Optional[float]
    output_path:    Optional[str]


class TriggerRunRequest(BaseModel):
    """Request body for triggering a new prospecting run."""
    max_queries:    int   = 5
    custom_search:  Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[GrantResultResponse])
def get_results(
    limit:        int            = Query(default=50, le=200),
    min_score:    Optional[float] = Query(default=None),
    current_user: User           = Depends(get_current_user),
):
    """
    Returns the most recent ranked grant prospect list
    for the current user's organization.

    Results are loaded from the most recent CSV export
    in the outputs directory.

    Args:
        limit:        Maximum number of results to return.
        min_score:    Optional minimum score filter.
        current_user: Authenticated user (auto-injected).

    Returns:
        List of GrantResultResponse objects.
    """
    results = _load_latest_results(current_user.org_name)

    if not results:
        return []

    # Apply score filter
    if min_score is not None:
        results = [
            r for r in results
            if r.get("score_final") and
            float(r["score_final"]) >= min_score
        ]

    # Apply limit
    results = results[:limit]

    return [_dict_to_result_response(r, i+1) for i, r in enumerate(results)]


@router.get("/summary")
def get_results_summary(
    current_user: User = Depends(get_current_user),
):
    """
    Returns a summary of the latest prospecting run.

    Used by the portal dashboard to show quick stats
    without loading the full results list.

    Args:
        current_user: Authenticated user.

    Returns:
        Dictionary with run summary statistics.
    """
    results = _load_latest_results(current_user.org_name)

    if not results:
        return {
            "total_results":    0,
            "top_score":        None,
            "avg_score":        None,
            "last_run":         None,
            "message":          "No results found. Run the agent to generate prospects.",
        }

    scores = [
        float(r["score_final"])
        for r in results
        if r.get("score_final") and str(r["score_final"]).replace(".","").isdigit()
    ]

    return {
        "total_results": len(results),
        "top_score":     max(scores) if scores else None,
        "avg_score":     round(sum(scores) / len(scores), 2) if scores else None,
        "last_run":      results[0].get("date_found") if results else None,
        "message":       f"Showing {len(results)} opportunities ranked by fit score.",
    }


@router.get("/export/csv")
def export_csv(
    current_user: User = Depends(get_current_user),
):
    """
    Downloads the most recent results as a CSV file.

    Args:
        current_user: Authenticated user.

    Returns:
        CSV file download response.

    Raises:
        HTTPException 404: No results file found.
    """
    csv_path = _find_latest_export(current_user.org_name, "csv")

    if not csv_path:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "No results file found. Run the agent first.",
        )

    return FileResponse(
        path             = csv_path,
        media_type       = "text/csv",
        filename         = Path(csv_path).name,
    )


@router.get("/export/excel")
def export_excel(
    current_user: User = Depends(get_current_user),
):
    """
    Downloads the most recent results as an Excel file.

    Args:
        current_user: Authenticated user.

    Returns:
        Excel file download response.

    Raises:
        HTTPException 404: No results file found.
    """
    excel_path = _find_latest_export(current_user.org_name, "xlsx")

    if not excel_path:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "No Excel file found. Run the agent first.",
        )

    return FileResponse(
        path       = excel_path,
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename   = Path(excel_path).name,
    )


@router.get("/runs", response_model=list[RunSummaryResponse])
def get_run_history(
    limit:        int  = Query(default=10, le=50),
    current_user: User = Depends(get_current_user),
):
    """
    Returns the prospecting run history for this organization.

    Args:
        limit:        Maximum number of runs to return.
        current_user: Authenticated user.

    Returns:
        List of RunSummaryResponse objects.
    """
    try:
        profile = OrgProfile.find_for_org(current_user.org_name)
        if not profile:
            return []

        state    = AgentState(profile)
        history  = state.get_run_history(limit=limit)

        return [
            RunSummaryResponse(
                run_date      = entry.get("timestamp", "Unknown"),
                total_results = entry.get("final_results", 0),
                top_score     = None,
                output_path   = entry.get("output_path"),
            )
            for entry in history
        ]
    except Exception as e:
        print(f"[Results] Error loading run history: {e}")
        return []


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
def trigger_run(
    request:        TriggerRunRequest,
    background:     BackgroundTasks,
    current_admin:  User    = Depends(get_current_admin),
    db:             Session = Depends(get_db),
):
    """
    Triggers a new grant prospecting run (Admin only).

    The run is dispatched as a background task and the endpoint returns
    immediately with HTTP 202 Accepted. A real run touches the network,
    invokes LLM scoring, and writes exports — easily multiple minutes —
    so running it inside the request thread would exceed any reasonable
    HTTP timeout. Poll GET /results/runs to see when it completes; the
    AgentState run history is the canonical record.

    Args:
        request:        Run parameters.
        background:     FastAPI BackgroundTasks (auto-injected).
        current_admin:  Must be Admin (auto-injected).
        db:             Database session.

    Returns:
        202 Accepted with a confirmation message. NOT the run result.
    """
    profile = OrgProfile.find_for_org(current_admin.org_name)
    if not profile:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"No profile found for organization: {current_admin.org_name}",
        )

    custom_queries = [request.custom_search] if request.custom_search else None

    background.add_task(
        _execute_run_in_background,
        profile        = profile,
        max_queries    = request.max_queries,
        custom_queries = custom_queries,
        triggered_by   = current_admin.email,
    )

    log.info(
        "Run queued for %s by %s (max_queries=%d)",
        profile.org_name, current_admin.email, request.max_queries,
    )

    return {
        "status":  "accepted",
        "message": (
            "Run queued. The agent is working in the background — this "
            "typically takes a few minutes. Poll GET /results/runs for "
            "completion and GET /results for the latest opportunities."
        ),
    }


def _execute_run_in_background(
    profile,
    max_queries:    int,
    custom_queries: Optional[list[str]],
    triggered_by:   str,
) -> None:
    """
    Background worker for /results/run.

    Runs synchronously inside FastAPI's BackgroundTasks executor, which is
    fine for a single-worker dev portal. For multi-worker / multi-host
    production, swap this for a real job queue (RQ / arq / Celery) so runs
    survive worker restarts and can be retried.

    Exceptions are caught and logged — never re-raised — because there is
    no caller to receive them and an unhandled exception in a background
    task can crash the worker.
    """
    try:
        from agent.loop import AgentLoop
        from output.formatter import ResultFormatter
        from output.exporter import ResultExporter

        log.info("Background run starting for %s (by %s)", profile.org_name, triggered_by)

        loop      = AgentLoop(profile)
        formatter = ResultFormatter(profile)
        exporter  = ResultExporter(profile)

        results = loop.run(
            max_queries    = max_queries,
            custom_queries = custom_queries,
        )

        if not results:
            log.info("Background run complete for %s — no new opportunities", profile.org_name)
            return

        formatted = formatter.format_all(results)
        exporter.export_csv(formatted)
        exporter.export_excel(formatted)
        exporter.export_run_summary(formatted)
        log.info(
            "Background run complete for %s — %d opportunities exported",
            profile.org_name, len(results),
        )
    except Exception:
        log.exception("Background run failed for %s (triggered by %s)", profile.org_name, triggered_by)


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_latest_results(org_name: str) -> list[dict]:
    """
    Loads the most recent results from the outputs directory.

    Args:
        org_name: Organization name to load results for.

    Returns:
        List of result dictionaries or empty list.
    """
    import csv

    org_slug = org_name.lower().replace(" ", "_").replace("'", "")
    base_dir = Path("outputs") / org_slug

    if not base_dir.exists():
        return []

    # Find the most recent CSV file
    csv_files = sorted(
        base_dir.rglob("grant_prospects_*.csv"),
        reverse = True
    )

    if not csv_files:
        return []

    try:
        results = []
        with open(csv_files[0], "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                results.append(dict(row))
        return results
    except Exception as e:
        print(f"[Results] Error loading CSV: {e}")
        return []


def _find_latest_export(org_name: str, extension: str) -> Optional[str]:
    """
    Finds the most recent export file of the given type.

    Args:
        org_name:   Organization name.
        extension:  File extension to look for: csv or xlsx.

    Returns:
        Path to the most recent file or None if not found.
    """
    org_slug = org_name.lower().replace(" ", "_").replace("'", "")
    base_dir = Path("outputs") / org_slug

    if not base_dir.exists():
        return None

    pattern = f"grant_prospects_*.{extension}"
    files   = sorted(base_dir.rglob(pattern), reverse=True)

    return str(files[0]) if files else None


def _dict_to_result_response(
    r: dict,
    rank: int
) -> GrantResultResponse:
    """
    Converts a CSV row dictionary to a GrantResultResponse.

    Args:
        r:    Dictionary from CSV row.
        rank: Display rank number.

    Returns:
        GrantResultResponse object.
    """
    def safe_float(val):
        try:
            return float(val) if val and val != "Not scored" else None
        except (ValueError, TypeError):
            return None

    def safe_int(val):
        try:
            return int(val) if val and val != "Unknown" else None
        except (ValueError, TypeError):
            return None

    return GrantResultResponse(
        rank                     = rank,
        funder_name              = r.get("funder_name", ""),
        program_name             = r.get("program_name", ""),
        score_final              = safe_float(r.get("score_final")),
        score_composite          = safe_float(r.get("score_composite")),
        application_deadline     = r.get("application_deadline"),
        days_remaining           = safe_int(r.get("days_remaining")),
        award_range              = r.get("award_range"),
        next_action              = r.get("next_action"),
        is_prior_funder          = r.get("is_prior_funder"),
        geographic_focus         = r.get("geographic_focus"),
        eligibility_requirements = r.get("eligibility_requirements"),
        application_url          = r.get("application_url"),
        score_geographic         = safe_float(r.get("score_geographic")),
        score_population         = safe_float(r.get("score_population")),
        score_budget             = safe_float(r.get("score_budget")),
        score_timeline           = safe_float(r.get("score_timeline")),
        reason_geographic        = r.get("reason_geographic"),
        reason_population        = r.get("reason_population"),
        reason_budget            = r.get("reason_budget"),
        reason_timeline          = r.get("reason_timeline"),
        source                   = r.get("source"),
        date_found               = r.get("date_found"),
    )