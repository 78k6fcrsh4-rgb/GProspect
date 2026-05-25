"""
tests/test_phase2_discovery.py
------------------------------
Phase 2 acceptance — ProPublica client + discovery cycle + funders router.

The ProPublica HTTP layer is stubbed via a FakeClient so tests don't hit
the live API. Discovery scoring + DB upsert behavior is exercised
end-to-end through the FastAPI TestClient.
"""

from __future__ import annotations

from typing import Iterable
from unittest.mock import patch

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


def _profile_payload(state: str = "OH", program: str = "education") -> dict:
    return {
        "org_name":           "Found Village",
        "org_short_name":     "FV",
        "ein":                "31-1234567",
        "ntee_codes":         ["P30"],
        "website":            None,
        "founded_year":       2014,
        "mission_statement":  "We support youth in the welfare system always.",
        "mission_keywords":   [],
        "program_areas":      [program],
        "program_descriptions": {},
        "populations_served": ["youth"],
        "geography": {"city": "Cincinnati", "state": state,
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


def _save_profile(client, token, payload=None):
    payload = payload or _profile_payload()
    r = client.post("/orgs/me/profile/version",
                    json={"profile": payload}, headers=_auth(token))
    assert r.status_code == 201, r.text


# ─────────────────────────────────────────────────────────────────────────────
# Fake ProPublica client — used by discovery tests
# ─────────────────────────────────────────────────────────────────────────────

class FakeOrg:
    def __init__(self, ein, name, state, ntee_code, city="Cincinnati", zipcode="45202"):
        self.ein         = ein
        self.raw_ein     = int(ein.replace("-", "")) if ein else None
        self.name        = name
        self.sub_name    = None
        self.address     = "123 Test"
        self.city        = city
        self.state       = state
        self.zipcode     = zipcode
        self.subseccd    = 3
        self.ntee_code   = ntee_code
        self.guidestar_url = None
        self.nccs_url      = None

    @property
    def ntee_letter(self):
        return self.ntee_code[0].upper() if self.ntee_code else None

    @property
    def is_foundation(self):
        return self.ntee_letter == "T"


class FakeFiling:
    def __init__(self, ein, year, assets, revenue=100_000, expenses=80_000):
        self.ein          = ein
        self.tax_prd      = year * 100 + 12
        self.tax_prd_yr   = year
        self.formtype     = 2
        self.totrevenue   = revenue
        self.totfuncexpns = expenses
        self.totassetsend = assets
        self.totliabend   = 0
        self.pdf_url      = None
        self.updated      = None
        self.extra        = {}


class FakeDetail:
    def __init__(self, org, filings):
        self.organization         = org
        self.filings_with_data    = filings
        self.filings_without_data = []


class FakeProPublicaClient:
    """
    Hand-rolled stub. Returns a curated set of orgs from iter_search() and
    keyed details from get_organization(). Lets us exercise the discovery
    cycle deterministically.
    """

    def __init__(self, orgs: list[FakeOrg], details: dict[str, FakeDetail]):
        self._orgs    = orgs
        self._details = details
        self.search_calls = 0
        self.detail_calls = 0

    def iter_search(self, max_results=100, **filters) -> Iterable[FakeOrg]:
        self.search_calls += 1
        # Filter by state if provided, mimicking the real client
        state = filters.get("state")
        ntee_major = filters.get("ntee_major")
        for org in self._orgs:
            if state and org.state and org.state.upper() != state.upper():
                continue
            if ntee_major is not None:
                # Use NTEE_LETTER_TO_MAJOR mapping. Since this is a stub for
                # tests, accept T-codes and human-services orgs broadly.
                from tools.propublica import NTEE_LETTER_TO_MAJOR
                expected = NTEE_LETTER_TO_MAJOR.get(org.ntee_letter or "")
                if expected != ntee_major:
                    continue
            yield org
            if max_results and len(list([]) ) >= max_results:
                break

    def get_organization(self, ein: str):
        self.detail_calls += 1
        if ein not in self._details:
            raise KeyError(f"No fake detail for EIN {ein}")
        return self._details[ein]


@pytest.fixture()
def fake_client():
    """A reasonable set of fake orgs to discover."""
    orgs = [
        # Community foundation in OH — should score high
        FakeOrg("31-0001111", "Greater Cincinnati Foundation", "OH", "T31"),
        # Private grantmaking foundation in OH — should score well
        FakeOrg("31-0002222", "Schott Foundation",            "OH", "T22"),
        # Foundation in IL — wrong state, should still appear but lower
        FakeOrg("36-0003333", "Chicago Community Trust",       "IL", "T31"),
        # Public charity (not a foundation) — should NOT be a candidate
        FakeOrg("31-0004444", "United Way of Cincinnati",      "OH", "P20"),
        # Foundation in OH, small assets
        FakeOrg("31-0005555", "Tiny Foundation",                "OH", "T20"),
        # Foundation in OH classified under Education — should be picked up in pass B
        FakeOrg("31-0006666", "Education Foundation of OH",    "OH", "T22"),
    ]
    details = {
        # Big assets (well above 50× request_ceiling=250k → 12.5M)
        "31-0001111": FakeDetail(orgs[0], [FakeFiling("31-0001111", 2025, 50_000_000)]),
        "31-0002222": FakeDetail(orgs[1], [FakeFiling("31-0002222", 2024, 25_000_000)]),
        "36-0003333": FakeDetail(orgs[2], [FakeFiling("36-0003333", 2025,  100_000_000)]),
        "31-0005555": FakeDetail(orgs[4], [FakeFiling("31-0005555", 2020,  500_000)]),  # stale + tiny
        "31-0006666": FakeDetail(orgs[5], [FakeFiling("31-0006666", 2024, 10_000_000)]),
    }
    return FakeProPublicaClient(orgs, details)


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper tests
# ─────────────────────────────────────────────────────────────────────────────

def test_propublica_format_ein():
    from tools.propublica import _format_ein, _normalize_ein_for_url
    assert _format_ein(311234567) == "31-1234567"
    assert _format_ein(None)      is None
    # 7-digit raw int pads to "00-XXXXXXX"; docs example "01-0191203" is raw_ein=10191203
    assert _format_ein(10191203)  == "01-0191203"
    assert _format_ein(1191203)   == "00-1191203"
    assert _normalize_ein_for_url("31-1234567") == "311234567"
    assert _normalize_ein_for_url(311234567)    == "311234567"


def test_parse_organization_defensive():
    from tools.propublica import _parse_organization
    org = _parse_organization({
        "ein": 311234567, "strein": "31-1234567",
        "name": "Test Org", "state": "OH",
        "ntee_code": "T22", "subseccd": 3,
    })
    assert org.ein == "31-1234567"
    assert org.ntee_letter == "T"
    assert org.is_foundation is True

    # Malformed input shouldn't raise
    org2 = _parse_organization({})
    assert org2.name is None
    assert org2.ein is None


# ─────────────────────────────────────────────────────────────────────────────
# Discovery cycle — full integration with FakeClient + DB
# ─────────────────────────────────────────────────────────────────────────────

def test_discovery_inserts_candidates_filtered_to_foundations(client, db_session, fake_client):
    deborahs = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=deborahs)
    token    = _login(client, admin.email, p)
    _save_profile(client, token, _profile_payload(state="OH", program="education"))

    from agent.discovery import discover_funders
    from agent.profile   import OrgProfile

    profile = OrgProfile.model_validate(_profile_payload(state="OH", program="education"))
    result = discover_funders(
        db      = db_session,
        org_id  = deborahs.id,
        profile = profile,
        client  = fake_client,
    )
    db_session.commit()

    # Foundations in OH: 31-0001111, 31-0002222, 31-0005555, 31-0006666 = 4
    # (Chicago Community Trust is IL — excluded by state filter)
    # (United Way is P-code — not a foundation, excluded)
    assert result.candidates_inserted == 4
    assert result.candidates_refreshed == 0

    # Re-run: should refresh, not duplicate
    result2 = discover_funders(
        db      = db_session,
        org_id  = deborahs.id,
        profile = profile,
        client  = fake_client,
    )
    db_session.commit()
    assert result2.candidates_inserted  == 0
    assert result2.candidates_refreshed == 4


def test_discovery_scoring_ranks_community_foundation_highest(client, db_session, fake_client):
    deborahs = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=deborahs)
    token    = _login(client, admin.email, p)
    _save_profile(client, token)

    from agent.discovery               import discover_funders
    from agent.profile                 import OrgProfile
    from portal.models.funder_candidate import FunderCandidate

    profile = OrgProfile.model_validate(_profile_payload(state="OH"))
    discover_funders(
        db      = db_session,
        org_id  = deborahs.id,
        profile = profile,
        client  = fake_client,
    )
    db_session.commit()

    cands = (
        db_session.query(FunderCandidate)
                  .filter(FunderCandidate.org_id == deborahs.id)
                  .order_by(FunderCandidate.score.desc())
                  .all()
    )
    # Greater Cincinnati Foundation should be the top score: T31 + OH match + big assets + recent
    top = cands[0]
    assert top.ein == "31-0001111"
    assert "Community foundation" in (top.rationale or "")
    assert top.signals.get("is_community_fdn") is True
    assert top.signals.get("state_match")      is True

    # Tiny foundation should rank lowest (stale + asset size too small)
    bottom = cands[-1]
    assert bottom.ein == "31-0005555"


def test_discovery_preserves_user_status_decision(client, db_session, fake_client):
    """Re-running discovery must NOT reset a candidate the user dismissed."""
    deborahs = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=deborahs)
    token    = _login(client, admin.email, p)
    _save_profile(client, token)

    from agent.discovery               import discover_funders
    from agent.profile                 import OrgProfile
    from portal.models.funder_candidate import FunderCandidate, CandidateStatus, set_status

    profile = OrgProfile.model_validate(_profile_payload(state="OH"))
    discover_funders(db=db_session, org_id=deborahs.id, profile=profile, client=fake_client)
    db_session.commit()

    # User dismisses one candidate
    set_status(db_session, org_id=deborahs.id, ein="31-0005555",
               status=CandidateStatus.DISMISSED, user_id=admin.id)
    db_session.commit()

    # Re-run discovery
    discover_funders(db=db_session, org_id=deborahs.id, profile=profile, client=fake_client)
    db_session.commit()

    tiny = (
        db_session.query(FunderCandidate)
                  .filter(FunderCandidate.org_id == deborahs.id,
                          FunderCandidate.ein    == "31-0005555")
                  .one()
    )
    assert tiny.status == CandidateStatus.DISMISSED


# ─────────────────────────────────────────────────────────────────────────────
# Router endpoints
# ─────────────────────────────────────────────────────────────────────────────

def test_list_candidates_hides_dismissed_by_default(client, db_session, fake_client):
    deborahs = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=deborahs)
    token    = _login(client, admin.email, p)
    _save_profile(client, token)

    from agent.discovery import discover_funders
    from agent.profile   import OrgProfile

    discover_funders(
        db      = db_session,
        org_id  = deborahs.id,
        profile = OrgProfile.model_validate(_profile_payload()),
        client  = fake_client,
    )
    db_session.commit()

    # Dismiss one
    r = client.post("/funders/31-0005555/status",
                    json={"status": "dismissed"},
                    headers=_auth(token))
    assert r.status_code == 200

    # Default list excludes dismissed
    r = client.get("/funders/candidates", headers=_auth(token))
    eins = {c["ein"] for c in r.json()}
    assert "31-0005555" not in eins

    # status=all includes them
    r = client.get("/funders/candidates?pursuit_status=all", headers=_auth(token))
    eins = {c["ein"] for c in r.json()}
    assert "31-0005555" in eins


def test_candidate_isolation_between_orgs(client, db_session, fake_client):
    deborahs   = make_org(db_session, "deborahs-place", "Deborah's Place")
    found_vill = make_org(db_session, "found-village",  "Found Village")
    alice, pa  = make_user(db_session, email="alice@dp.org", org=deborahs)
    bob,   pb  = make_user(db_session, email="bob@fv.org",   org=found_vill)
    ta = _login(client, alice.email, pa)
    tb = _login(client, bob.email,   pb)
    _save_profile(client, ta, _profile_payload(state="IL"))
    _save_profile(client, tb, _profile_payload(state="OH"))

    from agent.discovery import discover_funders
    from agent.profile   import OrgProfile
    discover_funders(db=db_session, org_id=deborahs.id,
                     profile=OrgProfile.model_validate(_profile_payload(state="IL")),
                     client=fake_client)
    discover_funders(db=db_session, org_id=found_vill.id,
                     profile=OrgProfile.model_validate(_profile_payload(state="OH")),
                     client=fake_client)
    db_session.commit()

    a_eins = {c["ein"] for c in client.get("/funders/candidates", headers=_auth(ta)).json()}
    b_eins = {c["ein"] for c in client.get("/funders/candidates", headers=_auth(tb)).json()}

    # Alice in IL gets Chicago Community Trust; Bob in OH gets the OH foundations.
    assert "36-0003333" in a_eins
    assert "31-0001111" in b_eins
    assert a_eins.isdisjoint(b_eins)


def test_discovery_run_returns_202_with_profile(client, db_session, fake_client):
    deborahs = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=deborahs)
    token    = _login(client, admin.email, p)
    _save_profile(client, token)

    # The endpoint dispatches a BackgroundTask; we just verify 202 + the
    # body. Patching SessionLocal in the worker so it sees our test DB
    # would be invasive — leave the worker as no-op-on-empty-search.
    with patch("portal.routers.funders.ProPublicaClient", return_value=fake_client):
        r = client.post("/discovery/run", headers=_auth(token))
    assert r.status_code == 202
    assert "queued" in r.json().get("message", "").lower()


def test_discovery_run_requires_saved_profile(client, db_session):
    deborahs = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=deborahs)
    token    = _login(client, admin.email, p)
    # NOT saving a profile

    r = client.post("/discovery/run", headers=_auth(token))
    assert r.status_code == 400


def test_non_admin_cannot_run_discovery(client, db_session):
    deborahs = make_org(db_session, "found-village", "Found Village")
    plain, p = make_user(db_session, email="staff@fv.org", org=deborahs, role="user")
    token    = _login(client, plain.email, p)

    r = client.post("/discovery/run", headers=_auth(token))
    assert r.status_code == 403


def test_invalid_candidate_status_returns_400(client, db_session, fake_client):
    deborahs = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=deborahs)
    token    = _login(client, admin.email, p)
    _save_profile(client, token)
    from agent.discovery import discover_funders
    from agent.profile   import OrgProfile
    discover_funders(db=db_session, org_id=deborahs.id,
                     profile=OrgProfile.model_validate(_profile_payload()),
                     client=fake_client)
    db_session.commit()

    r = client.post("/funders/31-0001111/status",
                    json={"status": "nope"},
                    headers=_auth(token))
    assert r.status_code == 400
