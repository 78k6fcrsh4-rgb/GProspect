"""
portal/routers/orchestrator.py
------------------------------
Phase 4b — orchestrator control plane.

  GET  /orchestrator/status              — scheduled jobs (with next fire
                                            time) + recent runs
  POST /orchestrator/trigger/{job_name}  — admin-only manual fire.
                                            Returns 202; runs via
                                            BackgroundTasks.

Admin-only across the board — scheduler internals shouldn't be exposed
to read-only users.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from agent.orchestrator                  import (
    ALL_JOBS,
    JOB_DISPATCH,
    fire_job_now,
    get_scheduler,
)
from database.db                         import get_db
from portal.auth.dependencies            import get_current_admin
from portal.models.scheduled_run         import ScheduledRun, recent_runs
from portal.models.user                  import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/orchestrator", tags=["Orchestrator"])


# ─────────────────────────────────────────────────────────────────────────────
# GET /orchestrator/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status")
def get_orchestrator_status(
    runs_limit:    int     = 50,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    """
    Returns:
      - enabled (bool): whether the scheduler is currently running
      - jobs:  list of {id, name, next_fire_time, trigger} for each
               registered job (empty when disabled)
      - recent_runs: last N ScheduledRun rows, newest first
    """
    scheduler = get_scheduler()
    enabled = scheduler is not None and scheduler.running

    jobs: list[dict] = []
    if scheduler is not None:
        for job in scheduler.get_jobs():
            jobs.append({
                "id":             job.id,
                "name":           job.name,
                "next_fire_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger":        str(job.trigger),
            })

    runs = recent_runs(db, limit=runs_limit)
    return {
        "enabled":     enabled,
        "jobs":        jobs,
        "recent_runs": [r.to_dict() for r in runs],
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /orchestrator/trigger/{job_name}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/trigger/{job_name}", status_code=status.HTTP_202_ACCEPTED)
def trigger_job(
    job_name:      str,
    background:    BackgroundTasks,
    current_admin: User    = Depends(get_current_admin),
):
    """
    Manually fire one of the orchestrator jobs. Returns 202 immediately;
    the job runs in BackgroundTasks (same pattern as /results/run and
    /discovery/run). The job logs its own ScheduledRun rows as it goes
    so the admin UI shows progress.
    """
    if job_name not in JOB_DISPATCH:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = f"Unknown job. Valid: {', '.join(ALL_JOBS)}",
        )

    background.add_task(fire_job_now, job_name)
    log.info("Job %s manually fired by %s", job_name, current_admin.email)
    return {
        "status":  "accepted",
        "message": (
            f"Job {job_name!r} queued. Poll GET /orchestrator/status for "
            f"completion (it will appear in recent_runs)."
        ),
    }
