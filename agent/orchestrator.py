"""
agent/orchestrator.py
---------------------
Phase 4b — the orchestrator. APScheduler-based, in-process.

Three scheduled jobs:

  nightly_health_check (default: 4:00 UTC daily)
    Pings ProPublica + IRS S3 with cheap requests to verify the upstream
    services are reachable. Logs a ScheduledRun row with org_id=NULL.
    Failing the health check does NOT crash the scheduler — just records
    the failure for the admin UI to surface.

  weekly_discovery (default: Monday 6:00 UTC)
    For every active org that has a saved profile, run discover_funders.
    One ScheduledRun row per (job_name, org).

  biweekly_grants_ingestion (default: 1st and 15th, 6:00 UTC)
    For every active org with at least one non-dismissed FunderCandidate,
    run ingest_for_org. One ScheduledRun row per (job_name, org).

Per-org exceptions are caught + logged so a single bad org doesn't kill
the whole fire. The scheduler itself stays alive across job failures.

Configuration:
  ORCHESTRATOR_ENABLED   ('1' to enable, anything else disables)
  ORCHESTRATOR_HEALTH_CRON     (default: '0 4 * * *')
  ORCHESTRATOR_DISCOVERY_CRON  (default: '0 6 * * 1')
  ORCHESTRATOR_INGEST_CRON     (default: '0 6 1,15 * *')
  ORCHESTRATOR_JOBSTORE_URL    (default: 'sqlite:///./orchestrator_jobs.db')

Single-worker assumption: APScheduler runs inside the FastAPI process.
With uvicorn --workers N you'd get N schedulers firing the same jobs.
For production multi-worker, the right move is to externalize to RQ/arq
(a Phase 5+ concern), or run a single dedicated worker process for the
scheduler and others handling HTTP.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron        import CronTrigger

from database.db                      import SessionLocal
from portal.models.scheduled_run      import (
    RunStatus,
    log_run_finished,
    log_run_started,
)

log = logging.getLogger(__name__)


JOB_HEALTH_CHECK     = "nightly_health_check"
JOB_DISCOVERY        = "weekly_discovery"
JOB_GRANTS_INGESTION = "biweekly_grants_ingestion"
JOB_SOURCE_CHECK     = "daily_source_check"

ALL_JOBS = (JOB_HEALTH_CHECK, JOB_DISCOVERY, JOB_GRANTS_INGESTION, JOB_SOURCE_CHECK)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CRON_HEALTH    = "0 4 * * *"      # daily 04:00 UTC
DEFAULT_CRON_DISCOVERY = "0 6 * * 1"      # Monday 06:00 UTC
DEFAULT_CRON_INGEST    = "0 6 1,15 * *"   # 1st and 15th of month, 06:00 UTC
DEFAULT_CRON_SOURCES   = "0 3 * * *"      # daily 03:00 UTC

DEFAULT_JOBSTORE_URL   = "sqlite:///./orchestrator_jobs.db"


def _enabled() -> bool:
    return os.getenv("ORCHESTRATOR_ENABLED", "1").strip().lower() in {"1", "true", "yes"}


def _cron_or_default(env_key: str, default: str) -> CronTrigger:
    raw = os.getenv(env_key, default).strip()
    try:
        return CronTrigger.from_crontab(raw)
    except Exception as e:
        log.warning("Invalid cron expression %r for %s; using default %r: %s",
                    raw, env_key, default, e)
        return CronTrigger.from_crontab(default)


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler factory + lifecycle
# ─────────────────────────────────────────────────────────────────────────────

_SCHEDULER: Optional[BackgroundScheduler] = None


def get_scheduler() -> Optional[BackgroundScheduler]:
    """Returns the currently-running scheduler, or None if disabled."""
    return _SCHEDULER


def start_scheduler() -> Optional[BackgroundScheduler]:
    """
    Build and start the BackgroundScheduler. Idempotent: a second call
    while the first is running returns the existing instance.

    Returns None if ORCHESTRATOR_ENABLED is unset/falsy — useful for
    tests + first-time setups that want to skip scheduling.
    """
    global _SCHEDULER
    if _SCHEDULER is not None and _SCHEDULER.running:
        return _SCHEDULER
    if not _enabled():
        log.info("Orchestrator disabled (ORCHESTRATOR_ENABLED != '1'). "
                 "No scheduled jobs will fire.")
        return None

    jobstore_url = os.getenv("ORCHESTRATOR_JOBSTORE_URL", DEFAULT_JOBSTORE_URL)
    scheduler = BackgroundScheduler(
        jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url)},
        timezone  = "UTC",
    )

    scheduler.add_job(
        run_health_check_job,
        trigger        = _cron_or_default("ORCHESTRATOR_HEALTH_CRON", DEFAULT_CRON_HEALTH),
        id             = JOB_HEALTH_CHECK,
        name           = "Daily upstream-source health check",
        replace_existing = True,
    )
    scheduler.add_job(
        run_discovery_job,
        trigger        = _cron_or_default("ORCHESTRATOR_DISCOVERY_CRON", DEFAULT_CRON_DISCOVERY),
        id             = JOB_DISCOVERY,
        name           = "Weekly per-org ProPublica discovery",
        replace_existing = True,
    )
    scheduler.add_job(
        run_grants_ingestion_job,
        trigger        = _cron_or_default("ORCHESTRATOR_INGEST_CRON", DEFAULT_CRON_INGEST),
        id             = JOB_GRANTS_INGESTION,
        name           = "Biweekly per-org IRS 990 ingestion",
        replace_existing = True,
    )
    scheduler.add_job(
        run_source_check_job,
        trigger        = _cron_or_default("ORCHESTRATOR_SOURCES_CRON", DEFAULT_CRON_SOURCES),
        id             = JOB_SOURCE_CHECK,
        name           = "Daily monitored-source check",
        replace_existing = True,
    )
    scheduler.start()
    _SCHEDULER = scheduler
    log.info("Orchestrator started. Jobstore: %s", jobstore_url)
    for j in scheduler.get_jobs():
        log.info("Scheduled %s — next fire: %s", j.id, j.next_run_time)
    return scheduler


def stop_scheduler() -> None:
    """Graceful shutdown. Safe to call multiple times."""
    global _SCHEDULER
    if _SCHEDULER is None or not _SCHEDULER.running:
        _SCHEDULER = None
        return
    try:
        _SCHEDULER.shutdown(wait=False)
    except Exception:
        log.exception("Error shutting down orchestrator")
    _SCHEDULER = None


# ─────────────────────────────────────────────────────────────────────────────
# Job implementations
#
# All three jobs are defined at module level so APScheduler's SQLAlchemy
# jobstore can pickle their references. Keep them as plain functions —
# no closures over scheduler state.
# ─────────────────────────────────────────────────────────────────────────────

def run_health_check_job() -> None:
    """
    Cheap ping of upstream services. Doesn't take meaningful time when
    things are up; logs a failed run when one is unreachable.
    """
    db = SessionLocal()
    try:
        row = log_run_started(db, job_name=JOB_HEALTH_CHECK, org_id=None)
        db.commit()
        try:
            messages = _check_upstreams()
            log_run_finished(db, row,
                              status  = RunStatus.SUCCESS,
                              message = "; ".join(messages) or "OK")
            db.commit()
        except Exception as e:
            log_run_finished(db, row,
                              status  = RunStatus.FAILED,
                              message = f"{type(e).__name__}: {e}")
            db.commit()
            log.exception("Health check job failed")
    finally:
        db.close()


def _check_upstreams() -> list[str]:
    """
    Send small requests to ProPublica + the IRS index endpoint to verify
    the orchestrator's upstream paths are reachable. Returns a list of
    "<source>: <status>" strings.
    """
    import requests
    messages: list[str] = []

    for label, url in [
        ("propublica", "https://projects.propublica.org/nonprofits/api/v2/search.json?q=propublica"),
        # The IRS bucket: HEAD on an index file is cheap. Pick a stable year.
        ("irs_990",    "https://s3.amazonaws.com/irs-form-990/index_2023.csv"),
    ]:
        try:
            resp = requests.head(url, timeout=15, allow_redirects=True)
            if resp.status_code == 405:
                # Some endpoints don't accept HEAD — fall back to a 1-byte GET.
                resp = requests.get(url, timeout=15, stream=True)
                resp.close()
            messages.append(f"{label}: HTTP {resp.status_code}")
        except Exception as e:
            messages.append(f"{label}: {type(e).__name__}")
    return messages


def run_discovery_job() -> None:
    """
    Weekly discovery for each active org with a current profile.
    """
    from agent.discovery               import discover_funders
    from agent.profile                 import OrgProfile
    from portal.models.organization    import Organization, OrgStatus
    from portal.models.org_profile     import get_current_for_org

    db = SessionLocal()
    try:
        orgs = (
            db.query(Organization)
              .filter(Organization.status == OrgStatus.ACTIVE)
              .all()
        )
        for org in orgs:
            current = get_current_for_org(db, org.id)
            if current is None:
                continue   # no profile → nothing to discover against

            row = log_run_started(db, job_name=JOB_DISCOVERY, org_id=org.id)
            db.commit()
            try:
                profile = OrgProfile.model_validate(current.payload or {})
                result  = discover_funders(db=db, org_id=org.id, profile=profile)
                db.commit()
                log_run_finished(
                    db, row,
                    status  = RunStatus.SUCCESS,
                    message = (
                        f"seen={result.candidates_seen}, "
                        f"new={result.candidates_inserted}, "
                        f"refreshed={result.candidates_refreshed}"
                    ),
                )
                db.commit()
            except Exception as e:
                log.exception("Discovery failed for org_id=%s", org.id)
                db.rollback()
                # Re-open a fresh transaction for the failure log
                row = (
                    db.query(type(row)).filter_by(id=row.id).one()
                )
                log_run_finished(db, row,
                                  status  = RunStatus.FAILED,
                                  message = f"{type(e).__name__}: {e}")
                db.commit()
    finally:
        db.close()


def run_grants_ingestion_job() -> None:
    """
    Biweekly grants ingestion for each active org with at least one
    active FunderCandidate.
    """
    from agent.grants_ingestion          import ingest_for_org
    from portal.models.funder_candidate  import CandidateStatus, FunderCandidate
    from portal.models.organization      import Organization, OrgStatus

    db = SessionLocal()
    try:
        orgs = (
            db.query(Organization)
              .filter(Organization.status == OrgStatus.ACTIVE)
              .all()
        )
        for org in orgs:
            has_active_cand = db.query(FunderCandidate).filter(
                FunderCandidate.org_id == org.id,
                FunderCandidate.status != CandidateStatus.DISMISSED,
            ).count() > 0
            if not has_active_cand:
                continue

            row = log_run_started(db, job_name=JOB_GRANTS_INGESTION, org_id=org.id)
            db.commit()
            try:
                result = ingest_for_org(db=db, org_id=org.id)
                db.commit()
                log_run_finished(
                    db, row,
                    status  = RunStatus.SUCCESS,
                    message = (
                        f"funders={result.funders_indexed}, "
                        f"grants_new={result.grants_inserted}, "
                        f"grants_refreshed={result.grants_refreshed}, "
                        f"missing={result.filings_missing}, "
                        f"failed={result.filings_failed}"
                    ),
                )
                db.commit()
            except Exception as e:
                log.exception("Grants ingestion failed for org_id=%s", org.id)
                db.rollback()
                row = (
                    db.query(type(row)).filter_by(id=row.id).one()
                )
                log_run_finished(db, row,
                                  status  = RunStatus.FAILED,
                                  message = f"{type(e).__name__}: {e}")
                db.commit()
    finally:
        db.close()


def run_source_check_job() -> None:
    """
    Daily monitored-source check. Iterates every enabled MonitoredSource
    (own + global) across all orgs and runs check_source on each.
    Per-source exceptions are caught + logged; one bad source doesn't
    poison the rest of the fire.

    Each (source) check produces:
      - one SourceCheck audit row (in the sources table)
      - one ScheduledRun row tying this orchestrator fire to the source's
        outcome, with org_id set so per-tenant filtering works
    """
    from portal.models.source       import MonitoredSource
    from tools.source_monitor       import check_source

    db = SessionLocal()
    try:
        sources = (
            db.query(MonitoredSource)
              .filter(MonitoredSource.enabled.is_(True))
              .all()
        )
        for source in sources:
            row = log_run_started(
                db, job_name=JOB_SOURCE_CHECK, org_id=source.org_id,
            )
            db.commit()
            try:
                result = check_source(db, source)
                db.commit()
                log_run_finished(
                    db, row,
                    status  = RunStatus.SUCCESS,
                    message = (
                        f"source={source.name} status={result.status.value} "
                        f"items={result.items_found}"
                    ),
                )
                db.commit()
            except Exception as e:
                log.exception("Source check failed for source_id=%s", source.id)
                db.rollback()
                row = (
                    db.query(type(row)).filter_by(id=row.id).one()
                )
                log_run_finished(
                    db, row,
                    status  = RunStatus.FAILED,
                    message = f"{type(e).__name__}: {e}",
                )
                db.commit()
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Manual fire — called by the router's POST /orchestrator/trigger
# ─────────────────────────────────────────────────────────────────────────────

JOB_DISPATCH = {
    JOB_HEALTH_CHECK:     run_health_check_job,
    JOB_DISCOVERY:        run_discovery_job,
    JOB_GRANTS_INGESTION: run_grants_ingestion_job,
    JOB_SOURCE_CHECK:     run_source_check_job,
}


def fire_job_now(job_name: str) -> None:
    """Synchronously run one of the orchestrator jobs. Used by manual trigger."""
    fn = JOB_DISPATCH.get(job_name)
    if fn is None:
        raise ValueError(f"Unknown job: {job_name}")
    fn()
