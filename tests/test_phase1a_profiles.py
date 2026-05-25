"""
tests/test_phase1a_profiles.py
------------------------------
Phase 1a acceptance — profile version endpoints + doc-assist extract.

Covers:
  - POST /orgs/me/profile/version validates against the Pydantic schema
    and rejects bad payloads with 422.
  - First save creates version 1 with is_current=True.
  - Subsequent saves increment the version number and demote the prior
    is_current=True row.
  - GET /orgs/me/profile/current returns the active version.
  - GET /orgs/me/profile/history returns all versions newest-first.
  - Cross-org access on profile endpoints returns 403 / no leak.
  - /profile/extract rejects unsupported file types and oversize uploads.
  - Doc-assist text-extraction helpers handle .txt and graceful failure
    on unsupported extensions.

The /extract endpoint's Claude call is stubbed by patching the function
so tests don't need an Anthropic API key and stay fast.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from tests.conftest import make_org, make_user


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _login(client, email, password):
    resp = client.post("/auth/login", data={"username": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _valid_profile(name="Found Village", city="Cincinnati", state="OH"):
    """Minimal payload that passes OrgProfile validation."""
    return {
        "org_name":           name,
        "org_short_name":     name,
        "ein":                "31-1234567",
        "ntee_codes":         ["P30"],
        "website":            "https://foundvillage.org",
        "founded_year":       2014,
        "mission_statement":  "We support youth in the welfare system " * 2,
        "mission_keywords":   ["youth", "foster care"],
        "program_areas":      ["education", "general_operating"],
        "program_descriptions": {"education": "Tutoring + life skills"},
        "populations_served": ["youth", "low_income"],
        "geography": {
            "city": city, "state": state,
            "county": None, "region": "Greater Cincinnati", "national": False,
        },
        "budget": {
            "request_floor":   10000,
            "request_ceiling": 100000,
            "annual_budget":   500000,
        },
        "known_funders": [],
        "funder_exclusions": [],
        "funder_type_exclusions": [],
        "settings": {
            "exclude_federal":       True,
            "exclude_state":         False,
            "deadline_floor_days":   14,
            "deadline_ceiling_days": 365,
            "min_composite_score":   2.0,
            "discovery_cycle_day":   "monday",
            "relationship_map_day":  1,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# /profile/version save flow
# ─────────────────────────────────────────────────────────────────────────────

def test_first_save_creates_version_1(client, db_session):
    org      = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@foundvillage.org", org=org)
    token    = _login(client, admin.email, p)

    resp = client.post(
        "/orgs/me/profile/version",
        json    = {"profile": _valid_profile()},
        headers = _auth(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"]    == 1
    assert body["is_current"] is True
    assert body["payload"]["org_name"] == "Found Village"


def test_second_save_increments_and_demotes(client, db_session):
    org      = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@foundvillage.org", org=org)
    token    = _login(client, admin.email, p)

    # Save v1
    r1 = client.post("/orgs/me/profile/version",
                     json={"profile": _valid_profile()},
                     headers=_auth(token))
    assert r1.status_code == 201
    assert r1.json()["version"] == 1

    # Save v2 with a tweak
    payload2 = _valid_profile()
    payload2["mission_keywords"] = ["youth", "foster care", "aging out"]
    r2 = client.post("/orgs/me/profile/version",
                     json={"profile": payload2},
                     headers=_auth(token))
    assert r2.status_code == 201
    body2 = r2.json()
    assert body2["version"]    == 2
    assert body2["is_current"] is True

    # /current should now be v2
    rc = client.get("/orgs/me/profile/current", headers=_auth(token))
    assert rc.status_code == 200
    assert rc.json()["version"] == 2

    # /history should list both, newest first
    rh = client.get("/orgs/me/profile/history", headers=_auth(token))
    assert rh.status_code == 200
    versions = rh.json()
    assert [v["version"] for v in versions] == [2, 1]
    # Only v2 should be current
    assert [v["is_current"] for v in versions] == [True, False]


def test_invalid_payload_returns_422(client, db_session):
    org      = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@foundvillage.org", org=org)
    token    = _login(client, admin.email, p)

    bad = _valid_profile()
    bad["mission_statement"] = "too short"      # < 20 chars → invalid
    bad["budget"]["request_ceiling"] = 1000     # ceiling < floor → invalid

    resp = client.post("/orgs/me/profile/version",
                       json={"profile": bad},
                       headers=_auth(token))
    assert resp.status_code == 422


def test_non_admin_cannot_save_profile(client, db_session):
    """A 'user' role can read but not save profile versions."""
    org      = make_org(db_session, "found-village", "Found Village")
    plain, p = make_user(db_session, email="staff@foundvillage.org",
                         org=org, role="user")
    token    = _login(client, plain.email, p)

    resp = client.post("/orgs/me/profile/version",
                       json={"profile": _valid_profile()},
                       headers=_auth(token))
    assert resp.status_code == 403


def test_current_404_when_no_profile_saved_yet(client, db_session):
    org      = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@foundvillage.org", org=org)
    token    = _login(client, admin.email, p)

    resp = client.get("/orgs/me/profile/current", headers=_auth(token))
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Cross-org isolation on profile endpoints
# ─────────────────────────────────────────────────────────────────────────────

def test_profile_isolation_between_orgs(client, db_session):
    deborahs   = make_org(db_session, "deborahs-place", "Deborah's Place")
    found_vill = make_org(db_session, "found-village",  "Found Village")

    alice, pwa = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    bob,   pwb = make_user(db_session, email="bob@foundvillage.org",    org=found_vill)
    alice_token = _login(client, alice.email, pwa)
    bob_token   = _login(client, bob.email,   pwb)

    # Alice saves Deborah's Place profile
    p = _valid_profile(name="Deborah's Place", city="Chicago", state="IL")
    p["program_areas"]      = ["housing_permanent", "housing_transitional"]
    p["populations_served"] = ["women", "chronically_homeless"]
    p["ntee_codes"]         = ["L41"]
    resp = client.post("/orgs/me/profile/version", json={"profile": p},
                       headers=_auth(alice_token))
    assert resp.status_code == 201

    # Bob saves Found Village profile (different city, different programs)
    p = _valid_profile()  # defaults to Found Village
    resp = client.post("/orgs/me/profile/version", json={"profile": p},
                       headers=_auth(bob_token))
    assert resp.status_code == 201

    # Bob's /current must NOT show Deborah's Place data
    rb = client.get("/orgs/me/profile/current", headers=_auth(bob_token))
    assert rb.status_code == 200
    assert rb.json()["payload"]["org_name"] == "Found Village"

    # Alice's /current must NOT show Found Village
    ra = client.get("/orgs/me/profile/current", headers=_auth(alice_token))
    assert ra.status_code == 200
    assert ra.json()["payload"]["org_name"] == "Deborah's Place"

    # Each /history shows only its own versions
    ha = client.get("/orgs/me/profile/history", headers=_auth(alice_token)).json()
    hb = client.get("/orgs/me/profile/history", headers=_auth(bob_token)).json()
    assert len(ha) == 1
    assert len(hb) == 1


# ─────────────────────────────────────────────────────────────────────────────
# /profile/extract endpoint
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_rejects_unsupported_file_type(client, db_session):
    org      = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@foundvillage.org", org=org)
    token    = _login(client, admin.email, p)

    resp = client.post(
        "/orgs/me/profile/extract",
        files   = {"file": ("readme.exe", b"binary garbage", "application/octet-stream")},
        headers = _auth(token),
    )
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


def test_extract_txt_with_stubbed_claude(client, db_session):
    org      = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@foundvillage.org", org=org)
    token    = _login(client, admin.email, p)

    sample_text = (
        b"Found Village is a Cincinnati-based nonprofit serving "
        b"young people in the welfare system."
    )

    # Stub the Claude call so the test doesn't need an API key.
    fake_extract = {
        "org_name":          "Found Village",
        "mission_statement": "Found Village is a Cincinnati-based nonprofit serving young people in the welfare system.",
        "populations_served": ["youth"],
        "geography":         {"city": "Cincinnati", "state": "OH"},
    }
    with patch(
        "portal.routers.profiles.extract_profile_fields_from_text",
        return_value=fake_extract,
    ):
        resp = client.post(
            "/orgs/me/profile/extract",
            files   = {"file": ("about.txt", sample_text, "text/plain")},
            headers = _auth(token),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["extracted_fields"]["org_name"] == "Found Village"
    assert body["extracted_fields"]["geography"]["city"] == "Cincinnati"
    # Notes should mention missing required fields (programs, budget, etc.)
    assert any("required" in n.lower() for n in body["notes"])


def test_extract_oversize_file_rejected(client, db_session):
    org      = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@foundvillage.org", org=org)
    token    = _login(client, admin.email, p)

    # 6 MB blob — over the 5 MB limit in agent/intake.py
    big = b"X" * (6 * 1024 * 1024)
    resp = client.post(
        "/orgs/me/profile/extract",
        files   = {"file": ("huge.txt", big, "text/plain")},
        headers = _auth(token),
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Standalone agent/intake helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_text_from_upload_txt():
    from agent.intake import extract_text_from_upload
    text = extract_text_from_upload(
        b"Hello\n\n\n\nWorld   \n",
        filename="note.txt",
    )
    # Trailing whitespace stripped, runs of blank lines collapsed.
    assert text == "Hello\n\nWorld"


def test_extract_text_from_upload_unsupported_extension_raises():
    from agent.intake import extract_text_from_upload, UnsupportedFileType
    with pytest.raises(UnsupportedFileType):
        extract_text_from_upload(b"data", "foo.bin")


def test_parse_json_lenient_strips_fences():
    """The helper should tolerate Claude's occasional ```json fences."""
    from agent.intake import _parse_json_lenient
    fenced = '```json\n{"org_name": "Found Village"}\n```'
    assert _parse_json_lenient(fenced) == {"org_name": "Found Village"}


def test_parse_json_lenient_handles_stray_text():
    from agent.intake import _parse_json_lenient
    noisy = 'Here is the extraction:\n{"a": 1, "b": "two"}\nThanks.'
    assert _parse_json_lenient(noisy) == {"a": 1, "b": "two"}
