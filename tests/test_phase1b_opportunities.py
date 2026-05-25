"""
tests/test_phase1b_opportunities.py
-----------------------------------
Phase 1b acceptance — opportunity helpers + pursuit endpoints + narrative
cache + digest payload.

The actual /opportunities and /digests endpoints depend on
`_load_latest_results(org_name)` reading CSVs from outputs/<slug>/. The
fixture below writes a tiny CSV into the per-test working directory so
the loader has something to read.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import make_org, make_user


# ─────────────────────────────────────────────────────────────────────────────
# Helpers + fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _login(client, email, password):
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


SAMPLE_ROWS = [
    {
        "funder_name":          "MacArthur Foundation",
        "program_name":         "Housing Equity Initiative",
        "score_final":          "4.5",
        "score_composite":      "4.5",
        "application_deadline": "2026-08-15",
        "days_remaining":       "20",
        "award_range":          "$100,000 - $500,000",
        "next_action":          "Submit LOI",
        "is_prior_funder":      "False",
        "geographic_focus":     "Chicago",
        "eligibility_requirements": "501(c)(3)",
        "application_url":      "https://example.org/macarthur",
        "score_geographic":     "1.0", "score_population": "0.9",
        "score_budget":         "0.95", "score_timeline": "0.7",
        "reason_geographic":    "Chicago focus",
        "reason_population":    "women's housing",
        "reason_budget":        "fits range",
        "reason_timeline":      "5 weeks",
        "source":               "test", "date_found":  "2026-05-23",
    },
    {
        "funder_name":          "Polk Bros Foundation",
        "program_name":         "Operating Support",
        "score_final":          "4.0",
        "score_composite":      "4.0",
        "application_deadline": "2026-09-01",
        "days_remaining":       "45",
        "award_range":          "$25,000 - $75,000",
        "next_action":          "Apply directly",
        "is_prior_funder":      "True",
        "geographic_focus":     "Chicago",
        "eligibility_requirements": "501(c)(3)",
        "application_url":      "https://example.org/polkbros",
        "score_geographic":     "1.0", "score_population": "0.8",
        "score_budget":         "0.8", "score_timeline": "0.9",
        "reason_geographic":    "Chicago", "reason_population": "low-income",
        "reason_budget":        "fits", "reason_timeline":  "ample",
        "source":               "test", "date_found":  "2026-05-23",
    },
    {
        "funder_name":          "Random Federal Agency",
        "program_name":         "Long-Distance Grant",
        "score_final":          "2.1",
        "score_composite":      "2.1",
        "application_deadline": "2027-01-15",
        "days_remaining":       "200",
        "award_range":          "$500,000 - $2,000,000",
        "next_action":          "Review eligibility",
        "is_prior_funder":      "False",
        "geographic_focus":     "National",
        "eligibility_requirements": "501(c)(3)",
        "application_url":      "https://example.org/fed",
        "score_geographic":     "0.3", "score_population": "0.6",
        "score_budget":         "0.4", "score_timeline": "0.5",
        "reason_geographic":    "national",
        "reason_population":    "broad",
        "reason_budget":        "too big",
        "reason_timeline":      "far out",
        "source":               "test", "date_found":  "2026-05-23",
    },
]


@pytest.fixture()
def csv_outputs(tmp_path, monkeypatch):
    """
    Lay down a grant_prospects CSV under outputs/<slug>/ and chdir to the
    parent so the production `_load_latest_results` (which uses relative
    Path("outputs")) finds it.
    """
    # The loader slugifies the org_name: lower, spaces→_, "'"→removed.
    # Match it for Deborah's Place AND Found Village.
    def _slug(name: str) -> str:
        return name.lower().replace(" ", "_").replace("'", "")

    for org_name in ("Deborah's Place", "Found Village"):
        org_dir = tmp_path / "outputs" / _slug(org_name) / "2026-05-23"
        org_dir.mkdir(parents=True, exist_ok=True)
        csv_path = org_dir / "grant_prospects_2026-05-23_000000.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(SAMPLE_ROWS[0].keys()))
            writer.writeheader()
            for row in SAMPLE_ROWS:
                writer.writerow(row)

    monkeypatch.chdir(tmp_path)
    return tmp_path


def _save_profile(client, token):
    """Save a minimal valid profile so the org has profile_version=1."""
    payload = {
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
    r = client.post("/orgs/me/profile/version", json={"profile": payload},
                    headers=_auth(token))
    assert r.status_code == 201, r.text


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper tests
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_opportunity_key_is_stable():
    from agent.opportunities import compute_opportunity_key
    k1 = compute_opportunity_key("MacArthur Foundation", "Housing Initiative")
    k2 = compute_opportunity_key("macarthur foundation",  "housing initiative")
    k3 = compute_opportunity_key("MacArthur  Foundation", " Housing Initiative ")
    assert k1 == k2 == k3
    assert len(k1) == 16


def test_classify_deadline_buckets():
    from agent.opportunities import classify_deadline
    assert classify_deadline(None) == "unknown"
    assert classify_deadline(-3)   == "past"
    assert classify_deadline(0)    == "hot"
    assert classify_deadline(30)   == "hot"
    assert classify_deadline(31)   == "warm"
    assert classify_deadline(60)   == "warm"
    assert classify_deadline(61)   == "cold"
    assert classify_deadline(500)  == "cold"


def test_parse_days_remaining_handles_garbage():
    from agent.opportunities import parse_days_remaining
    assert parse_days_remaining("47")      == 47
    assert parse_days_remaining(" 12 ")    == 12
    assert parse_days_remaining("Unknown") is None
    assert parse_days_remaining("")        is None
    assert parse_days_remaining(None)      is None


# ─────────────────────────────────────────────────────────────────────────────
# /opportunities — enriched list
# ─────────────────────────────────────────────────────────────────────────────

def test_list_opportunities_enriches_with_opp_key_and_bucket(client, db_session, csv_outputs):
    deborahs = make_org(db_session, "deborahs-place", "Deborah's Place")
    admin, p = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    token    = _login(client, admin.email, p)

    r = client.get("/opportunities/", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3

    macarthur = next(o for o in body if "MacArthur" in o["funder_name"])
    assert macarthur["opp_key"]
    assert macarthur["deadline_bucket"] == "hot"      # 20 days
    assert macarthur["score_final"]     == 4.5
    assert macarthur["pursuit"]         is None
    assert macarthur["has_narrative"]   is False

    federal = next(o for o in body if "Federal" in o["funder_name"])
    assert federal["deadline_bucket"] == "cold"      # 200 days


def test_list_opportunities_pursuit_filter(client, db_session, csv_outputs):
    deborahs = make_org(db_session, "deborahs-place", "Deborah's Place")
    admin, p = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    token    = _login(client, admin.email, p)

    # Mark MacArthur as Pursuing
    macarthur_row = client.get("/opportunities/", headers=_auth(token)).json()[0]
    client.post(
        f"/opportunities/{macarthur_row['opp_key']}/pursue",
        json    = {},
        headers = _auth(token),
    )

    # Filter to pursuing → 1 result
    r = client.get("/opportunities/?pursuit=pursuing", headers=_auth(token))
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert "MacArthur" in r.json()[0]["funder_name"]

    # Filter to new → 2 results (others)
    r = client.get("/opportunities/?pursuit=new", headers=_auth(token))
    assert r.status_code == 200
    assert len(r.json()) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Pursuit endpoints
# ─────────────────────────────────────────────────────────────────────────────

def test_pursuit_state_transitions(client, db_session, csv_outputs):
    deborahs = make_org(db_session, "deborahs-place", "Deborah's Place")
    admin, p = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    token    = _login(client, admin.email, p)

    opp = client.get("/opportunities/", headers=_auth(token)).json()[0]
    key = opp["opp_key"]

    # Pursue
    r = client.post(f"/opportunities/{key}/pursue", json={"notes": "go!"},
                    headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["status"] == "pursuing"
    assert r.json()["notes"]  == "go!"

    # Switch to watching
    r = client.post(f"/opportunities/{key}/watch", json={}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["status"] == "watching"

    # Clear
    r = client.post(f"/opportunities/{key}/clear", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["cleared"] is True

    # Clearing again is a no-op (False)
    r = client.post(f"/opportunities/{key}/clear", headers=_auth(token))
    assert r.json()["cleared"] is False


def test_pursuit_isolation_between_orgs(client, db_session, csv_outputs):
    deborahs   = make_org(db_session, "deborahs-place", "Deborah's Place")
    found_vill = make_org(db_session, "found-village",  "Found Village")
    alice, pa  = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    bob,   pb  = make_user(db_session, email="bob@foundvillage.org",    org=found_vill)
    ta = _login(client, alice.email, pa)
    tb = _login(client, bob.email,   pb)

    opp = client.get("/opportunities/", headers=_auth(ta)).json()[0]
    key = opp["opp_key"]

    # Alice pursues
    client.post(f"/opportunities/{key}/pursue", json={}, headers=_auth(ta))

    # Bob's view of the same opp_key → no pursuit row
    bob_list = client.get("/opportunities/", headers=_auth(tb)).json()
    bob_macarthur = next(o for o in bob_list if o["opp_key"] == key)
    assert bob_macarthur["pursuit"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Narrative endpoint — cache hit/miss with Claude stubbed
# ─────────────────────────────────────────────────────────────────────────────

def test_narrative_cache_hit_and_miss(client, db_session, csv_outputs):
    deborahs = make_org(db_session, "deborahs-place", "Deborah's Place")
    admin, p = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    token    = _login(client, admin.email, p)
    _save_profile(client, token)

    opp = client.get("/opportunities/", headers=_auth(token)).json()[0]
    key = opp["opp_key"]

    fake_narrative = type("X", (), {
        "conversational_md": "MacArthur funds Chicago housing. Strong fit.",
        "scored_breakdown":  {"geographic": {"score": 1.0, "reason": "Chicago"}},
        "model_used":        "stub-model",
    })()

    with patch(
        "portal.routers.opportunities.generate_narrative",
        return_value=fake_narrative,
    ) as gen:
        # First call — miss → 1 generation
        r1 = client.post(f"/opportunities/{key}/narrative", headers=_auth(token))
        assert r1.status_code == 200
        assert r1.json()["cached"] is False
        assert "MacArthur funds Chicago" in r1.json()["conversational_md"]

        # Second call — hit → no second generation
        r2 = client.post(f"/opportunities/{key}/narrative", headers=_auth(token))
        assert r2.status_code == 200
        assert r2.json()["cached"] is True
        assert gen.call_count == 1, "Expected single Claude call across cache hit + miss"


def test_narrative_requires_saved_profile(client, db_session, csv_outputs):
    deborahs = make_org(db_session, "deborahs-place", "Deborah's Place")
    admin, p = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    token    = _login(client, admin.email, p)
    # Intentionally NOT saving a profile.

    opp = client.get("/opportunities/", headers=_auth(token)).json()[0]
    r = client.post(f"/opportunities/{opp['opp_key']}/narrative", headers=_auth(token))
    assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Digest payload assembly + ZIP generation
# ─────────────────────────────────────────────────────────────────────────────

def test_digest_payload_picks_top_five_and_30d_calendar():
    from agent.digest import build_digest_payload

    opps = [
        {"rank": i, "opp_key": f"k{i}", "funder_name": f"F{i}", "program_name": f"P{i}",
         "score_final": 5.0 - i*0.2, "days_remaining": d, "application_deadline": "2026-08-01",
         "award_range": "$10k-$100k", "next_action": "...", "application_url": "https://x"}
        for i, d in enumerate([10, 20, 45, 70, 5, 100, 200, 8])
    ]
    payload = build_digest_payload(
        org_display_name = "Test Org",
        opportunities    = opps,
        profile_payload  = {"geography": {"city": "Chicago", "state": "IL"}},
    )
    assert payload["org"]["display_name"] == "Test Org"
    assert "Chicago" in payload["org"]["context"]
    assert len(payload["top_five"]) == 5
    # Calendar: only days ≤ 30 → 10, 20, 5, 8 → 4 entries sorted ascending
    cal_days = [d["days_remaining"] for d in payload["deadline_calendar"]]
    assert cal_days == sorted(cal_days)
    assert all(d <= 30 for d in cal_days)
    assert set(cal_days) == {5, 8, 10, 20}


def test_digest_renderers_produce_nonempty_output():
    """Smoke-render to verify python-docx + reportlab integration."""
    from agent.digest import build_digest_payload, render_docx, render_pdf

    payload = build_digest_payload(
        org_display_name = "Test Org",
        opportunities    = [{
            "rank": 1, "opp_key": "k1", "funder_name": "F1",
            "program_name": "P1", "score_final": 4.5, "days_remaining": 10,
            "application_deadline": "2026-08-01", "award_range": "$10k-$100k",
            "next_action": "Submit LOI", "application_url": "https://x",
        }],
        profile_payload = None,
    )
    docx_bytes = render_docx(payload)
    pdf_bytes  = render_pdf(payload)
    assert docx_bytes.startswith(b"PK")          # ZIP magic — DOCX is a zip
    assert pdf_bytes.startswith(b"%PDF-")        # PDF magic
    assert len(docx_bytes) > 1000
    assert len(pdf_bytes)  > 1000


def test_digest_endpoint_returns_zip(client, db_session, csv_outputs):
    deborahs = make_org(db_session, "deborahs-place", "Deborah's Place")
    admin, p = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    token    = _login(client, admin.email, p)
    _save_profile(client, token)

    r = client.post("/digests/generate", headers=_auth(token))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    # Verify the ZIP contains both files
    import zipfile, io
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert "weekly_digest.docx" in names
    assert "weekly_digest.pdf"  in names


def test_digest_requires_profile(client, db_session, csv_outputs):
    deborahs = make_org(db_session, "deborahs-place", "Deborah's Place")
    admin, p = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    token    = _login(client, admin.email, p)
    # No profile saved.
    r = client.post("/digests/generate", headers=_auth(token))
    assert r.status_code == 400


def test_digest_requires_opportunities(client, db_session, tmp_path, monkeypatch):
    """When no CSV exists for the org, digest 404s — no empty PDF returned."""
    monkeypatch.chdir(tmp_path)   # outputs/ is empty
    deborahs = make_org(db_session, "deborahs-place", "Deborah's Place")
    admin, p = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    token    = _login(client, admin.email, p)
    _save_profile(client, token)
    r = client.post("/digests/generate", headers=_auth(token))
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline conflict detector
# ─────────────────────────────────────────────────────────────────────────────

def test_deadline_conflict_detector():
    from frontend.prospects import _detect_deadline_conflicts
    opps = [
        {"funder_name": "A", "program_name": "p", "days_remaining": 10,
         "application_deadline": "2026-08-01"},
        {"funder_name": "B", "program_name": "p", "days_remaining": 13,
         "application_deadline": "2026-08-04"},   # 3 days from A → conflict
        {"funder_name": "C", "program_name": "p", "days_remaining": 30,
         "application_deadline": "2026-08-21"},   # 17 days from B → no conflict
    ]
    conflicts = _detect_deadline_conflicts(opps, window_days=7)
    assert len(conflicts) == 1
    a, b, gap = conflicts[0]
    assert a["funder_name"] == "A"
    assert b["funder_name"] == "B"
    assert gap == 3
