"""
tests/test_phase3b_warm_paths.py
--------------------------------
Phase 3b acceptance — peer-org matching + warm-path endpoints.

Covers:
  - expand_keywords: deduplicates and folds in mission/program/population sources
  - score_peer_match: state-only is borderline; state + keyword is solid;
    no state means not a peer regardless of name
  - find_peer_grants_for_funder: orders by score → year → amount, respects
    the user's state
  - /funders/{ein}/warm-paths: structures the response, returns empty
    gracefully when no profile / no ingested grants
  - /funders/warm-paths/summary: aggregates across the candidate pool,
    excludes dismissed candidates, ordered by peer count desc
  - Cross-org isolation: Alice's candidate pool never leaks into Bob's
    warm-path summary
"""

from __future__ import annotations

from datetime import datetime, timezone

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


def _profile_payload(state="OH",
                     city="Cincinnati",
                     program_areas=("education",),
                     populations=("youth",),
                     mission_keywords=("foster care", "aging out")):
    return {
        "org_name":           "Found Village",
        "org_short_name":     "FV",
        "ein":                "31-1234567",
        "ntee_codes":         ["P30"],
        "website":            None,
        "founded_year":       2014,
        "mission_statement":  "We support youth in the welfare system always.",
        "mission_keywords":   list(mission_keywords),
        "program_areas":      list(program_areas),
        "program_descriptions": {},
        "populations_served": list(populations),
        "geography": {"city": city, "state": state, "county": None,
                      "region": None, "national": False},
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


def _add_candidate(db, *, org_id, ein, name, state="OH"):
    from portal.models.funder_candidate import FunderCandidate, CandidateStatus
    fc = FunderCandidate(
        org_id       = org_id,
        ein          = ein,
        funder_name  = name,
        score        = 4.0,
        rationale    = "",
        signals      = {},
        status       = CandidateStatus.CANDIDATE,
        funder_state = state,
    )
    db.add(fc)
    db.flush()
    return fc


def _add_funder(db, *, ein, name, state="OH"):
    from portal.models.grant import Funder
    f = Funder(ein=ein, name=name, state=state, last_990pf_year=2024,
               last_ingested_at=datetime.now(timezone.utc))
    db.add(f)
    db.flush()
    return f


def _add_grant(db, *, funder, recipient, amount, year):
    from portal.models.grant import Grant
    g = Grant(
        funder_id    = funder.id,
        recipient_id = recipient.id,
        amount       = amount,
        fiscal_year  = year,
        tax_period   = year * 100 + 12,
        purpose      = "support",
        source_filing_object_id = f"obj_{funder.id}_{year}",
    )
    db.add(g)
    db.flush()
    return g


def _add_recipient(db, *, name, state="OH", city=None, ein=None):
    from portal.models.grant import upsert_recipient_org
    row, _ = upsert_recipient_org(db, name=name, state=state, city=city, ein=ein)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper tests
# ─────────────────────────────────────────────────────────────────────────────

def test_expand_keywords_combines_sources_and_dedupes():
    from agent.peer_match import expand_keywords
    from agent.profile    import OrgProfile

    p = OrgProfile.model_validate(_profile_payload(
        program_areas    = ("education",),
        populations      = ("youth", "families"),
        mission_keywords = ("youth", "literacy", "FOSTER care"),
    ))
    kws = expand_keywords(p)

    # Lowercased + deduped
    assert "youth" in kws
    assert kws.count("youth") == 1
    assert "foster care" in kws        # mission keyword preserved
    assert "literacy" in kws
    assert "education" in kws          # from PROGRAM_AREA_KEYWORDS
    assert "children" in kws           # from POPULATION_KEYWORDS["youth"]
    assert "family" in kws             # from POPULATION_KEYWORDS["families"]


def test_name_matches_keywords_word_boundary_and_phrase():
    from agent.peer_match import name_matches_keywords
    # Single token: word-boundary
    assert "youth" in name_matches_keywords("Cincinnati Youth Collaborative", ["youth"])
    assert "youth" not in name_matches_keywords("Mouthful Inc", ["youth"])
    # Multi-word phrase: substring
    assert "mental health" in name_matches_keywords(
        "Greater Mental Health Alliance", ["mental health"]
    )


def test_score_peer_match_state_required():
    from agent.peer_match  import score_peer_match
    from agent.profile     import OrgProfile
    from portal.models.grant import RecipientOrg

    profile = OrgProfile.model_validate(_profile_payload(state="OH", city="Cincinnati"))

    # Same state + name keyword → peer
    peer = RecipientOrg(name="Cincinnati Youth Collaborative", state="OH",
                       city="Cincinnati", normalized_name="cincinnati youth collaborative")
    s = score_peer_match(peer, profile)
    assert s.is_peer
    assert s.score >= 2.0   # state + keyword + city

    # Different state + keyword → NOT a peer
    other = RecipientOrg(name="Chicago Youth Coalition", state="IL",
                         normalized_name="chicago youth coalition")
    s = score_peer_match(other, profile)
    assert not s.is_peer

    # Same state, no keyword → still peer (state alone qualifies)
    bland = RecipientOrg(name="Local Animal Shelter", state="OH",
                         normalized_name="local animal shelter")
    s = score_peer_match(bland, profile)
    assert s.is_peer       # state alone is >= 1.5


# ─────────────────────────────────────────────────────────────────────────────
# find_peer_grants_for_funder
# ─────────────────────────────────────────────────────────────────────────────

def test_find_peer_grants_orders_and_filters_by_state(client, db_session):
    from agent.peer_match import find_peer_grants_for_funder
    from agent.profile    import OrgProfile

    org = make_org(db_session, "found-village", "Found Village")
    profile = OrgProfile.model_validate(_profile_payload(state="OH"))

    funder = _add_funder(db_session, ein="31-9990000", name="Test Foundation")

    # OH recipient with name match — top score
    r_oh_match = _add_recipient(db_session, name="Cincinnati Youth Collaborative",
                                 state="OH", city="Cincinnati")
    g_oh_match = _add_grant(db_session, funder=funder, recipient=r_oh_match,
                             amount=200_000, year=2024)

    # OH recipient without name match — still a peer (state alone)
    r_oh_state = _add_recipient(db_session, name="Some Other OH Org", state="OH")
    g_oh_state = _add_grant(db_session, funder=funder, recipient=r_oh_state,
                             amount=50_000, year=2023)

    # IL recipient with name match — should NOT be a peer (different state)
    r_il = _add_recipient(db_session, name="Chicago Youth Alliance", state="IL")
    _add_grant(db_session, funder=funder, recipient=r_il, amount=300_000, year=2024)

    db_session.commit()

    hits = find_peer_grants_for_funder(db_session, profile, "31-9990000")
    assert [h.recipient_name for h in hits] == [
        "Cincinnati Youth Collaborative",   # higher score (state + kw + city)
        "Some Other OH Org",                 # state-only
    ]
    assert hits[0].score > hits[1].score
    assert hits[0].fiscal_year == 2024
    assert hits[0].amount      == 200_000


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

def test_warm_paths_endpoint_returns_peers(client, db_session):
    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)
    _save_profile(client, token)

    _add_candidate(db_session, org_id=org.id, ein="31-9990001",
                   name="Test Foundation")
    funder = _add_funder(db_session, ein="31-9990001", name="Test Foundation")
    r = _add_recipient(db_session, name="Cincinnati Youth Collaborative",
                        state="OH", city="Cincinnati")
    _add_grant(db_session, funder=funder, recipient=r, amount=150_000, year=2024)
    db_session.commit()

    resp = client.get("/funders/31-9990001/warm-paths", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["funder_name"] == "Test Foundation"
    assert len(body["peer_grants"]) == 1
    pg = body["peer_grants"][0]
    assert pg["recipient_name"] == "Cincinnati Youth Collaborative"
    assert pg["amount"]         == 150_000
    assert pg["fiscal_year"]    == 2024
    assert "based in OH" in pg["reasons"][0]


def test_warm_paths_endpoint_404_when_candidate_missing(client, db_session):
    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)
    _save_profile(client, token)

    r = client.get("/funders/99-9999999/warm-paths", headers=_auth(token))
    assert r.status_code == 404


def test_warm_paths_endpoint_handles_no_profile(client, db_session):
    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)
    # Profile intentionally NOT saved

    _add_candidate(db_session, org_id=org.id, ein="31-0001",
                   name="Whatever")
    db_session.commit()
    r = client.get("/funders/31-0001/warm-paths", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["peer_grants"] == []
    assert "profile" in (body.get("note") or "").lower()


def test_warm_paths_summary_orders_and_excludes_dismissed(client, db_session):
    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)
    _save_profile(client, token)

    # Two active candidates + one dismissed
    _add_candidate(db_session, org_id=org.id, ein="31-A", name="Active A")
    _add_candidate(db_session, org_id=org.id, ein="31-B", name="Active B")
    dismissed = _add_candidate(db_session, org_id=org.id, ein="31-D",
                                name="Dismissed")
    from portal.models.funder_candidate import CandidateStatus
    dismissed.status = CandidateStatus.DISMISSED

    # Active A: 2 peer grants
    f_a = _add_funder(db_session, ein="31-A", name="Active A")
    r1  = _add_recipient(db_session, name="Cincinnati Youth Collaborative",
                          state="OH", city="Cincinnati")
    r2  = _add_recipient(db_session, name="Other OH Nonprofit", state="OH")
    _add_grant(db_session, funder=f_a, recipient=r1, amount=100_000, year=2024)
    _add_grant(db_session, funder=f_a, recipient=r2, amount=50_000,  year=2023)
    # Active B: 1 peer grant
    f_b = _add_funder(db_session, ein="31-B", name="Active B")
    _add_grant(db_session, funder=f_b, recipient=r2, amount=20_000,  year=2024)
    # Dismissed: should NOT appear in summary
    f_d = _add_funder(db_session, ein="31-D", name="Dismissed")
    _add_grant(db_session, funder=f_d, recipient=r1, amount=500_000, year=2024)

    db_session.commit()

    r = client.get("/funders/warm-paths/summary", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    eins = [row["funder_ein"] for row in body]
    assert "31-D" not in eins                       # dismissed excluded
    # Active A (count=2) comes before Active B (count=1)
    assert eins == ["31-A", "31-B"]
    assert body[0]["peer_grant_count"] == 2
    assert body[1]["peer_grant_count"] == 1


def test_warm_paths_cross_org_isolation(client, db_session):
    """Alice's candidate pool must not contribute to Bob's warm-path summary."""
    deborahs   = make_org(db_session, "deborahs-place", "Deborah's Place")
    found_vill = make_org(db_session, "found-village",  "Found Village")
    alice, pa  = make_user(db_session, email="alice@dp.org", org=deborahs)
    bob,   pb  = make_user(db_session, email="bob@fv.org",   org=found_vill)
    ta = _login(client, alice.email, pa)
    tb = _login(client, bob.email,   pb)
    _save_profile(client, ta, _profile_payload(state="IL", city="Chicago",
                                                program_areas=("housing_permanent",),
                                                populations=("women",)))
    _save_profile(client, tb, _profile_payload(state="OH"))

    # Candidate only in Found Village's pool, with peer grants in OH
    _add_candidate(db_session, org_id=found_vill.id, ein="31-FV", name="FV-only")
    f = _add_funder(db_session, ein="31-FV", name="FV-only")
    r = _add_recipient(db_session, name="Cincinnati Youth Collaborative",
                        state="OH")
    _add_grant(db_session, funder=f, recipient=r, amount=10_000, year=2024)
    db_session.commit()

    # Alice's summary should be empty (her org has no candidates here)
    ra = client.get("/funders/warm-paths/summary", headers=_auth(ta)).json()
    assert ra == []

    # Bob sees the FV-only candidate
    rb = client.get("/funders/warm-paths/summary", headers=_auth(tb)).json()
    assert any(row["funder_ein"] == "31-FV" for row in rb)


def test_warm_paths_empty_when_no_grants_ingested(client, db_session):
    """Candidate exists but no grant rows yet → empty list, no crash."""
    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)
    _save_profile(client, token)

    _add_candidate(db_session, org_id=org.id, ein="31-EMPTY", name="Empty Foundation")
    db_session.commit()

    r = client.get("/funders/31-EMPTY/warm-paths", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["peer_grants"] == []

    s = client.get("/funders/warm-paths/summary", headers=_auth(token)).json()
    assert s[0]["funder_ein"]       == "31-EMPTY"
    assert s[0]["peer_grant_count"] == 0
