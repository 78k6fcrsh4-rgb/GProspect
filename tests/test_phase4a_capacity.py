"""
tests/test_phase4a_capacity.py
------------------------------
Phase 4a acceptance — OrgCapacity model + capacity-aware ranking +
capacity endpoints.

Covers:
  - is_within_window: inclusive boundaries, malformed dates → False, no
    crash on None
  - get_or_default: creates a default row, idempotent
  - upsert: validates / drops malformed window rows
  - assess_opportunity: closed-window demote, at-capacity warning,
    over-capacity warning, already-pursuing skip
  - summarize_capacity: utilization + headroom + active windows + next
    future window
  - /orgs/me/capacity GET/PUT (admin-only)
  - /opportunities/capacity-summary
  - /opportunities/ includes capacity_fit per row, demoted score when
    deadline is inside a closed window
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib  import Path

import pytest

from tests.conftest import make_org, make_user


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _login(client, email, password):
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper tests
# ─────────────────────────────────────────────────────────────────────────────

def test_is_within_window_inclusive_boundaries():
    from portal.models.capacity import is_within_window
    windows = [{"start": "2026-12-15", "end": "2026-12-31", "label": "Freeze"}]

    in_, label = is_within_window("2026-12-15", windows)
    assert in_ is True and label == "Freeze"
    in_, label = is_within_window("2026-12-31", windows)
    assert in_ is True
    in_, label = is_within_window("2026-12-25", windows)
    assert in_ is True
    in_, _ = is_within_window("2026-12-14", windows)
    assert in_ is False
    in_, _ = is_within_window("2027-01-01", windows)
    assert in_ is False


def test_is_within_window_handles_garbage():
    from portal.models.capacity import is_within_window
    assert is_within_window(None, None)          == (False, None)
    assert is_within_window(None, [])            == (False, None)
    assert is_within_window("nope", [])          == (False, None)
    assert is_within_window("2026-12-15", [{"start": "x"}]) == (False, None)


def test_get_or_default_idempotent(db_session):
    from portal.models.capacity import get_or_default

    org = make_org(db_session, "found-village", "Found Village")
    r1 = get_or_default(db_session, org.id)
    db_session.commit()
    r2 = get_or_default(db_session, org.id)
    db_session.commit()
    assert r1.id == r2.id   # same row, no duplicate inserted


def test_upsert_drops_malformed_windows(db_session):
    from portal.models.capacity import upsert
    org = make_org(db_session, "found-village", "Found Village")

    row = upsert(
        db_session,
        org_id                 = org.id,
        active_pursuits_target = 4,
        availability_windows   = [
            {"start": "2026-12-15", "end": "2026-12-31", "label": "Freeze"},
            {"start": "",           "end": "2027-01-01"},     # dropped
            {"label": "no dates"},                              # dropped
            {"start": "2027-07-01", "end": "2027-08-15"},     # kept (default label)
        ],
        user_id = None,
    )
    db_session.commit()
    assert row.active_pursuits_target == 4
    assert len(row.availability_windows) == 2
    assert row.availability_windows[1]["label"] == "Closed window"


# ─────────────────────────────────────────────────────────────────────────────
# Re-ranking
# ─────────────────────────────────────────────────────────────────────────────

def test_assess_opportunity_closed_window_demotes():
    from agent.capacity_rank import assess_opportunity
    opp = {
        "application_deadline": "2026-12-20",
        "pursuit": None,
    }
    fit = assess_opportunity(
        opportunity          = opp,
        capacity_target      = 5,
        current_pursuing     = 2,
        availability_windows = [{"start": "2026-12-15", "end": "2027-01-05",
                                  "label": "Holiday freeze"}],
    )
    assert fit.fit_label        == "closed_window"
    assert fit.score_adjustment < 0
    assert any("Holiday freeze" in w for w in fit.warnings)


def test_assess_opportunity_at_capacity_warns():
    from agent.capacity_rank import assess_opportunity
    opp = {"application_deadline": "2027-06-15", "pursuit": None}
    fit = assess_opportunity(
        opportunity          = opp,
        capacity_target      = 5,
        current_pursuing     = 5,
        availability_windows = [],
    )
    assert fit.fit_label == "tight"
    assert any("capacity target" in w for w in fit.warnings)


def test_assess_opportunity_over_capacity_demotes_harder():
    from agent.capacity_rank import assess_opportunity
    opp = {"application_deadline": "2027-06-15", "pursuit": None}
    fit = assess_opportunity(
        opportunity          = opp,
        capacity_target      = 5,
        current_pursuing     = 7,
        availability_windows = [],
    )
    assert fit.fit_label == "over"
    assert fit.score_adjustment < -0.5


def test_assess_opportunity_already_pursuing_no_demote():
    """Don't demote work the user has already committed to."""
    from agent.capacity_rank import assess_opportunity
    opp = {
        "application_deadline": "2027-06-15",
        "pursuit": {"status": "pursuing"},
    }
    fit = assess_opportunity(
        opportunity          = opp,
        capacity_target      = 5,
        current_pursuing     = 10,   # way over
        availability_windows = [],
    )
    assert fit.fit_label        == "open"
    assert fit.score_adjustment == 0.0
    assert fit.warnings         == []


def test_summarize_capacity_computes_headroom_and_windows():
    from agent.capacity_rank import summarize_capacity
    s = summarize_capacity(
        capacity_target      = 5,
        current_pursuing     = 3,
        availability_windows = [
            {"start": "2030-12-15", "end": "2030-12-31", "label": "Far freeze"},
        ],
        today = date(2026, 6, 1),
    )
    assert s.active_pursuits_target == 5
    assert s.current_pursuing       == 3
    assert s.headroom               == 2
    assert s.utilization_pct        == 60.0
    assert s.closed_windows_active  == []
    assert s.next_closed_window["label"] == "Far freeze"


def test_summarize_capacity_detects_current_window():
    from agent.capacity_rank import summarize_capacity
    s = summarize_capacity(
        capacity_target      = 5,
        current_pursuing     = 1,
        availability_windows = [
            {"start": "2026-05-01", "end": "2026-12-31", "label": "Long freeze"},
        ],
        today = date(2026, 6, 1),
    )
    assert len(s.closed_windows_active) == 1
    assert s.closed_windows_active[0]["label"] == "Long freeze"


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

def test_get_capacity_inserts_default(client, db_session):
    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)

    r = client.get("/orgs/me/capacity", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["active_pursuits_target"] == 5
    assert body["availability_windows"]   == []


def test_put_capacity_admin_only(client, db_session):
    org = make_org(db_session, "found-village", "Found Village")
    plain, p = make_user(db_session, email="staff@fv.org", org=org, role="user")
    token = _login(client, plain.email, p)

    r = client.put("/orgs/me/capacity",
                   json={"active_pursuits_target": 4, "availability_windows": []},
                   headers=_auth(token))
    assert r.status_code == 403


def test_put_capacity_round_trip(client, db_session):
    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)

    r = client.put("/orgs/me/capacity",
                   json={
                       "active_pursuits_target": 3,
                       "availability_windows": [
                           {"start": "2026-12-15", "end": "2026-12-31",
                            "label": "Holiday freeze"},
                       ],
                   },
                   headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["active_pursuits_target"] == 3
    assert len(r.json()["availability_windows"]) == 1

    # GET should reflect the saved state
    r = client.get("/orgs/me/capacity", headers=_auth(token))
    assert r.json()["active_pursuits_target"] == 3


def test_capacity_summary_endpoint(client, db_session):
    from portal.models.opportunity     import OpportunityPursuit, PursuitStatus

    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)

    # Set a custom target + add 2 pursuing rows
    client.put("/orgs/me/capacity",
               json={"active_pursuits_target": 5, "availability_windows": []},
               headers=_auth(token))
    for i in range(2):
        p_row = OpportunityPursuit(
            org_id      = org.id,
            opp_key     = f"opp_{i}",
            funder_name = "T", program_name = "X",
            status      = PursuitStatus.PURSUING,
        )
        db_session.add(p_row)
    db_session.commit()

    r = client.get("/opportunities/capacity-summary", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["active_pursuits_target"] == 5
    assert body["current_pursuing"]       == 2
    assert body["headroom"]               == 3
    assert body["utilization_pct"]        == 40.0


# ─────────────────────────────────────────────────────────────────────────────
# /opportunities includes capacity_fit and demotes scores
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def csv_with_deadline_in_window(tmp_path, monkeypatch):
    """Write a one-row CSV whose deadline falls inside a known window."""
    def _slug(name): return name.lower().replace(" ", "_").replace("'", "")
    org_dir = tmp_path / "outputs" / _slug("Found Village") / "2026-12-01"
    org_dir.mkdir(parents=True, exist_ok=True)
    csv_path = org_dir / "grant_prospects_2026-12-01_000000.csv"
    headers = [
        "funder_name", "program_name", "score_final", "score_composite",
        "application_deadline", "days_remaining", "award_range", "next_action",
        "is_prior_funder", "geographic_focus", "eligibility_requirements",
        "application_url", "score_geographic", "score_population",
        "score_budget", "score_timeline", "reason_geographic",
        "reason_population", "reason_budget", "reason_timeline",
        "source", "date_found",
    ]
    row = {h: "" for h in headers}
    row.update({
        "funder_name":          "Freeze Foundation",
        "program_name":         "Holiday Program",
        "score_final":          "4.0",
        "application_deadline": "2026-12-20",
        "days_remaining":       "20",
        "award_range":          "$50k",
        "application_url":      "https://example.org",
        "source":               "test",
        "date_found":           "2026-12-01",
    })
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerow(row)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_opportunities_response_has_capacity_fit_and_demoted_score(
    client, db_session, csv_with_deadline_in_window,
):
    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)

    # Set a window that contains the CSV's deadline
    client.put("/orgs/me/capacity",
               json={
                   "active_pursuits_target": 5,
                   "availability_windows": [
                       {"start": "2026-12-15", "end": "2026-12-31",
                        "label": "Holiday freeze"},
                   ],
               },
               headers=_auth(token))

    r = client.get("/opportunities/", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    item = body[0]

    fit = item.get("capacity_fit")
    assert fit is not None
    assert fit["fit_label"] == "closed_window"
    # 4.0 base score - 1.5 closed-window demote = 2.5
    assert item["score_final"] < 4.0
    assert any("Holiday freeze" in w for w in fit["warnings"])


def test_capacity_cross_org_isolation(client, db_session):
    deborahs   = make_org(db_session, "deborahs-place", "Deborah's Place")
    found_vill = make_org(db_session, "found-village",  "Found Village")
    alice, pa  = make_user(db_session, email="alice@dp.org", org=deborahs)
    bob,   pb  = make_user(db_session, email="bob@fv.org",   org=found_vill)
    ta = _login(client, alice.email, pa)
    tb = _login(client, bob.email,   pb)

    # Alice sets target=2
    client.put("/orgs/me/capacity",
               json={"active_pursuits_target": 2, "availability_windows": []},
               headers=_auth(ta))
    # Bob sets target=8
    client.put("/orgs/me/capacity",
               json={"active_pursuits_target": 8, "availability_windows": []},
               headers=_auth(tb))

    a_target = client.get("/orgs/me/capacity", headers=_auth(ta)).json()["active_pursuits_target"]
    b_target = client.get("/orgs/me/capacity", headers=_auth(tb)).json()["active_pursuits_target"]
    assert a_target == 2
    assert b_target == 8
