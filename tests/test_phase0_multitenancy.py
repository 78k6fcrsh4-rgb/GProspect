"""
tests/test_phase0_multitenancy.py
---------------------------------
Phase 0 acceptance — two-org isolation.

These tests verify that the multi-tenant foundation works at the edges
that matter most:

  - Two organizations can exist concurrently with separate users.
  - GET /orgs/me returns the caller's org and nothing else.
  - GET /admin/users only lists users from the caller's org.
  - Cross-org user modification returns 403, not 404 (visibility should
    not leak through a 404-vs-403 distinction).

Each test is independent — it constructs its own orgs + users via the
conftest helpers, hits the API via the TestClient, and asserts behavior.
"""

from __future__ import annotations

from tests.conftest import make_org, make_user


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _login(client, email: str, password: str) -> str:
    """POST /auth/login and return the bearer token."""
    resp = client.post(
        "/auth/login",
        data = {"username": email, "password": password},
    )
    assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text}"
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_two_orgs_can_coexist(client, db_session):
    """Smoke: both pilot orgs can be created and have independent users."""
    deborahs   = make_org(db_session, "deborahs-place", "Deborah's Place")
    found_vill = make_org(db_session, "found-village",  "Found Village")

    alice, _ = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    bob,   _ = make_user(db_session, email="bob@foundvillage.org",   org=found_vill)

    assert alice.org_id == deborahs.id
    assert bob.org_id   == found_vill.id
    assert alice.org_id != bob.org_id


def test_orgs_me_returns_only_caller_org(client, db_session):
    """GET /orgs/me returns the caller's org row, no peeking at the other one."""
    deborahs   = make_org(db_session, "deborahs-place", "Deborah's Place")
    found_vill = make_org(db_session, "found-village",  "Found Village")
    alice, pw  = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    bob,   pwb = make_user(db_session, email="bob@foundvillage.org",   org=found_vill)

    alice_token = _login(client, alice.email, pw)
    bob_token   = _login(client, bob.email,   pwb)

    a_resp = client.get("/orgs/me", headers=_auth(alice_token))
    b_resp = client.get("/orgs/me", headers=_auth(bob_token))

    assert a_resp.status_code == 200
    assert b_resp.status_code == 200
    assert a_resp.json()["slug"] == "deborahs-place"
    assert b_resp.json()["slug"] == "found-village"
    # Sanity — the two responses do not see the other org's identity at all.
    assert a_resp.json()["id"] != b_resp.json()["id"]


def test_admin_users_only_lists_own_org(client, db_session):
    """GET /admin/users scopes by current_user.org_name — no cross-org leakage."""
    deborahs   = make_org(db_session, "deborahs-place", "Deborah's Place")
    found_vill = make_org(db_session, "found-village",  "Found Village")

    alice, pw_a  = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    alice2, _    = make_user(db_session, email="alice2@deborahsplace.org", org=deborahs)
    bob,   pw_b  = make_user(db_session, email="bob@foundvillage.org",   org=found_vill)
    bob2,  _     = make_user(db_session, email="bob2@foundvillage.org",  org=found_vill)

    alice_token = _login(client, alice.email, pw_a)
    bob_token   = _login(client, bob.email,   pw_b)

    a_resp = client.get("/admin/users", headers=_auth(alice_token))
    b_resp = client.get("/admin/users", headers=_auth(bob_token))

    assert a_resp.status_code == 200
    assert b_resp.status_code == 200
    a_emails = {u["email"] for u in a_resp.json()}
    b_emails = {u["email"] for u in b_resp.json()}

    assert a_emails == {"alice@deborahsplace.org", "alice2@deborahsplace.org"}
    assert b_emails == {"bob@foundvillage.org",    "bob2@foundvillage.org"}
    assert a_emails.isdisjoint(b_emails)


def test_cross_org_user_modification_returns_403(client, db_session):
    """
    Critical isolation test: Bob (Found Village admin) trying to deactivate
    Alice (Deborah's Place user) must return 403 — NOT 404.

    A 404 here would let an attacker probe for valid user IDs in other orgs;
    403 means "I see this resource exists but you can't touch it" which is
    exactly the wrong leak. The correct behavior is 403 because the route
    explicitly checks org_name and prefers leaking-no-information over
    distinguishing "not found" from "not yours."
    """
    deborahs   = make_org(db_session, "deborahs-place", "Deborah's Place")
    found_vill = make_org(db_session, "found-village",  "Found Village")

    alice, _    = make_user(db_session, email="alice@deborahsplace.org", org=deborahs)
    bob,   pwb  = make_user(db_session, email="bob@foundvillage.org",   org=found_vill)

    bob_token = _login(client, bob.email, pwb)
    resp = client.put(f"/admin/users/{alice.id}/deactivate", headers=_auth(bob_token))

    assert resp.status_code == 403, (
        f"Expected 403 (forbidden, see-but-cannot-touch) on cross-org "
        f"modification, got {resp.status_code}: {resp.text}"
    )

    # And confirm Alice is still active — the failed cross-org write didn't
    # have any side effect.
    db_session.refresh(alice)
    assert alice.is_active is True


def test_orgs_me_requires_auth(client, db_session):
    """Unauthenticated GET /orgs/me returns 401."""
    # No orgs/users — just verify the route enforces auth.
    resp = client.get("/orgs/me")
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for unauthenticated /orgs/me, got {resp.status_code}"
    )
