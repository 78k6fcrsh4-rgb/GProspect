"""
tests/test_phase4b_orchestrator.py
----------------------------------
Phase 4b acceptance — orchestrator jobs + dispatcher + endpoints.

The scheduler itself isn't started in tests (we set ORCHESTRATOR_ENABLED=0
in conftest implicitly by not exporting it). We test:
  - ScheduledRun helpers round-trip correctly
  - run_discovery_job + run_grants_ingestion_job iterate orgs, log per-org
    rows, and survive per-org exceptions
  - run_health_check_job catches network failures and records them as
    a single failed run
  - /orchestrator/status returns enabled=False + recent runs
  - /orchestrator/trigger fires the dispatcher (patched) and is admin-only
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_org, make_user


# Make doubly sure the scheduler doesn't auto-start during tests even if
# something else flips the env var. The unit tests below never call
# start_scheduler() directly.
os.environ["ORCHESTRATOR_ENABLED"] = "0"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _login(client, email, password):
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _profile_payload():
    return {
        "org_name":           "Found Village",
        "org_short_name":     "FV",
        "ein":                "31-1234567",
        "ntee_codes":         ["P30"],
        "website":            None,
        "founded_year":       2014,
        "mission_statement":  "We support youth in the welfare system always.",
        "mission_keywords":   [],
        "program_areas":      ["education"],
        "program_descriptions": {},
        "populations_served": ["youth"],
        "geography": {"city": "Cincinnati", "state": "OH",
                      "county": None, "region": None, "national": False},
        "budget": {"request_floor": 10000, "request_ceiling": 250000,
                   "annual_budget": None},
        "known_funders": [], "funder_exclusions": [],
        "funder_type_exclusions": [],
        "settings": {
            "exclude_federal": True, "exclude_state": False,
            "deadline_floor_days": 14, "deadline_ceiling_days": 365,
            "min_composite_score": 2.0,
            "discovery_cycle_day": "monday", "relationship_map_day": 1,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# ScheduledRun helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduled_run_log_round_trip(db_session):
    from portal.models.scheduled_run import (
        RunStatus, log_run_finished, log_run_started, recent_runs,
    )
    row = log_run_started(db_session, job_name="weekly_discovery", org_id=None)
    db_session.commit()
    assert row.status == RunStatus.STARTED
    assert row.duration_ms is None

    log_run_finished(db_session, row, status=RunStatus.SUCCESS, message="ok")
    db_session.commit()
    assert row.status      == RunStatus.SUCCESS
    assert row.message     == "ok"
    assert row.duration_ms is not None
    assert row.duration_ms >= 0

    rows = recent_runs(db_session, job_name="weekly_discovery", limit=10)
    assert len(rows) == 1
    assert rows[0].id == row.id


# ─────────────────────────────────────────────────────────────────────────────
# run_discovery_job — iterates orgs, logs per-org, survives failure
# ─────────────────────────────────────────────────────────────────────────────

def test_discovery_job_iterates_active_orgs_with_profile(client, db_session):
    """
    Active orgs with a saved profile each get a ScheduledRun row.
    Orgs without a profile are skipped (no row created).
    """
    from agent import orchestrator
    from portal.models.scheduled_run import ScheduledRun
    from portal.models.opportunity   import OpportunityPursuit       # noqa: F401  (used elsewhere)

    org_with    = make_org(db_session, "fv",    "Found Village")
    org_without = make_org(db_session, "other", "No-profile Org")
    admin, p = make_user(db_session, email="admin@fv.org", org=org_with)
    token    = _login(client, admin.email, p)

    # org_with gets a profile; org_without does not.
    r = client.post("/orgs/me/profile/version", json={"profile": _profile_payload()},
                    headers=_auth(token))
    assert r.status_code == 201

    fake_result = MagicMock(
        candidates_seen     = 5,
        candidates_inserted = 3,
        candidates_refreshed = 2,
        search_calls        = 1,
        detail_calls        = 0,
        notes               = [],
    )

    # Patch SessionLocal in the job so it uses our test engine, AND patch
    # discover_funders at its source module (the orchestrator imports it
    # lazily inside the job body, so we can't patch agent.orchestrator.*).
    with patch("agent.orchestrator.SessionLocal", return_value=db_session), \
         patch("agent.discovery.discover_funders", return_value=fake_result) as df:
        # The job opens/closes its session via SessionLocal(); when we return
        # the test session, .close() is called which would detach the bind.
        # Wrap to no-op out the close.
        db_session.close = lambda: None
        orchestrator.run_discovery_job()

    # discover_funders called for the one org with a profile, not the other.
    called_org_ids = [kwargs["org_id"] for _, kwargs in df.call_args_list]
    assert org_with.id    in called_org_ids
    assert org_without.id not in called_org_ids

    rows = (
        db_session.query(ScheduledRun)
                  .filter(ScheduledRun.job_name == "weekly_discovery")
                  .all()
    )
    assert len(rows) == 1
    assert rows[0].org_id == org_with.id
    assert rows[0].status.value == "success"
    assert "new=3" in (rows[0].message or "")


def test_discovery_job_survives_per_org_failure(client, db_session):
    """An exception in one org's discovery should not poison the next org."""
    from agent import orchestrator
    from portal.models.scheduled_run import ScheduledRun

    org1 = make_org(db_session, "org1", "Org One")
    org2 = make_org(db_session, "org2", "Org Two")
    admin1, p1 = make_user(db_session, email="a1@org1.org", org=org1)
    admin2, p2 = make_user(db_session, email="a2@org2.org", org=org2)
    t1 = _login(client, admin1.email, p1)
    t2 = _login(client, admin2.email, p2)
    client.post("/orgs/me/profile/version", json={"profile": _profile_payload()},
                headers=_auth(t1))
    client.post("/orgs/me/profile/version", json={"profile": _profile_payload()},
                headers=_auth(t2))

    fake_ok = MagicMock(
        candidates_seen=1, candidates_inserted=1, candidates_refreshed=0,
        search_calls=1, detail_calls=0, notes=[],
    )

    def fake_discover(*, db, org_id, profile):
        if org_id == org1.id:
            raise RuntimeError("simulated upstream failure")
        return fake_ok

    with patch("agent.orchestrator.SessionLocal", return_value=db_session), \
         patch("agent.discovery.discover_funders", side_effect=fake_discover):
        db_session.close = lambda: None
        orchestrator.run_discovery_job()

    rows = (
        db_session.query(ScheduledRun)
                  .filter(ScheduledRun.job_name == "weekly_discovery")
                  .order_by(ScheduledRun.org_id)
                  .all()
    )
    assert len(rows) == 2
    by_org = {r.org_id: r for r in rows}
    assert by_org[org1.id].status.value == "failed"
    assert "simulated upstream failure" in (by_org[org1.id].message or "")
    assert by_org[org2.id].status.value == "success"


# ─────────────────────────────────────────────────────────────────────────────
# run_grants_ingestion_job
# ─────────────────────────────────────────────────────────────────────────────

def test_grants_ingestion_job_skips_orgs_without_candidates(client, db_session):
    from agent import orchestrator
    from portal.models.scheduled_run import ScheduledRun
    from portal.models.funder_candidate import (
        CandidateStatus, FunderCandidate,
    )

    org_with    = make_org(db_session, "fv",    "Found Village")
    org_without = make_org(db_session, "other", "No-cands Org")
    db_session.add(FunderCandidate(
        org_id=org_with.id, ein="11-0000001", funder_name="X",
        score=1.0, rationale="", signals={}, status=CandidateStatus.CANDIDATE,
    ))
    db_session.commit()

    fake_ingest = MagicMock(
        funders_indexed=1, grants_inserted=2, grants_refreshed=0,
        filings_missing=0, filings_failed=0, notes=[],
    )

    with patch("agent.orchestrator.SessionLocal", return_value=db_session), \
         patch("agent.grants_ingestion.ingest_for_org", return_value=fake_ingest) as ing:
        db_session.close = lambda: None
        orchestrator.run_grants_ingestion_job()

    # Only the org with candidates was processed
    called_org_ids = [kwargs["org_id"] for _, kwargs in ing.call_args_list]
    assert called_org_ids == [org_with.id]

    rows = (
        db_session.query(ScheduledRun)
                  .filter(ScheduledRun.job_name == "biweekly_grants_ingestion")
                  .all()
    )
    assert len(rows) == 1
    assert rows[0].org_id == org_with.id
    assert "funders=1" in (rows[0].message or "")


# ─────────────────────────────────────────────────────────────────────────────
# run_health_check_job — survives upstream failure
# ─────────────────────────────────────────────────────────────────────────────

def test_health_check_records_a_run_even_on_failure(db_session):
    from agent import orchestrator
    from portal.models.scheduled_run import ScheduledRun

    # Make _check_upstreams raise — the wrapper should still log the run.
    def boom():
        raise ConnectionError("upstream down")

    with patch("agent.orchestrator.SessionLocal", return_value=db_session), \
         patch("agent.orchestrator._check_upstreams", side_effect=boom):
        db_session.close = lambda: None
        orchestrator.run_health_check_job()

    rows = (
        db_session.query(ScheduledRun)
                  .filter(ScheduledRun.job_name == "nightly_health_check")
                  .all()
    )
    assert len(rows) == 1
    assert rows[0].status.value == "failed"
    assert "ConnectionError" in (rows[0].message or "")


# ─────────────────────────────────────────────────────────────────────────────
# Manual fire dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def test_fire_job_now_unknown_raises():
    from agent.orchestrator import fire_job_now
    with pytest.raises(ValueError):
        fire_job_now("not_a_job")


def test_fire_job_now_dispatches_correctly():
    """
    JOB_DISPATCH captured references at module-import time, so patching
    the function name doesn't redirect through the dict. Patch the dict
    entry instead.
    """
    from agent.orchestrator import fire_job_now, JOB_DISPATCH
    fake = MagicMock()
    with patch.dict(JOB_DISPATCH, {"nightly_health_check": fake}):
        fire_job_now("nightly_health_check")
    fake.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Router endpoints
# ─────────────────────────────────────────────────────────────────────────────

def test_orchestrator_status_admin_only(client, db_session):
    org = make_org(db_session, "fv", "Found Village")
    plain, p = make_user(db_session, email="staff@fv.org", org=org, role="user")
    token = _login(client, plain.email, p)
    r = client.get("/orchestrator/status", headers=_auth(token))
    assert r.status_code == 403


def test_orchestrator_status_returns_disabled_in_tests(client, db_session):
    """No scheduler is started in test environments."""
    org = make_org(db_session, "fv", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)

    r = client.get("/orchestrator/status", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["jobs"] == []
    assert isinstance(body["recent_runs"], list)


def test_orchestrator_trigger_invalid_job(client, db_session):
    org = make_org(db_session, "fv", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)
    r = client.post("/orchestrator/trigger/nope", headers=_auth(token))
    assert r.status_code == 400


def test_orchestrator_trigger_valid_job_returns_202(client, db_session):
    org = make_org(db_session, "fv", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)

    # Patch the dispatcher so the BG task doesn't actually fire over the network.
    with patch("portal.routers.orchestrator.fire_job_now") as fire:
        r = client.post("/orchestrator/trigger/nightly_health_check",
                        headers=_auth(token))
    assert r.status_code == 202
    fire.assert_called_once_with("nightly_health_check")


def test_orchestrator_trigger_admin_only(client, db_session):
    org = make_org(db_session, "fv", "Found Village")
    plain, p = make_user(db_session, email="staff@fv.org", org=org, role="user")
    token = _login(client, plain.email, p)
    r = client.post("/orchestrator/trigger/nightly_health_check",
                    headers=_auth(token))
    assert r.status_code == 403
