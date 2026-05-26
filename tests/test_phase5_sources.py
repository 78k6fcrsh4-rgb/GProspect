"""
tests/test_phase5_sources.py
----------------------------
Phase 5 acceptance — source monitoring + custom parsers + endpoints.

The network layer is stubbed via patching tools.source_monitor._fetch
so tests don't hit any live foundation site. The custom parsers are
exercised against inline HTML fixtures.
"""

from __future__ import annotations

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


# ─────────────────────────────────────────────────────────────────────────────
# Inline HTML/XML fixtures
# ─────────────────────────────────────────────────────────────────────────────

# Realistic-ish foundation grants page — the structured pass should
# find both opportunities.
GCF_HTML = """
<!doctype html><html><body>
  <article>
    <h2><a href="/grants/safety-net">Safety Net Grant</a></h2>
    <p>Apply by March 15, 2026. Grants up to $50,000 for safety-net
    services in Greater Cincinnati.</p>
  </article>
  <article>
    <h3><a href="/grants/youth">Youth Opportunity Fund</a></h3>
    <p>Request for proposals for youth-focused nonprofits.
    Deadline: May 1, 2026.</p>
  </article>
  <div class="footer">Just navigation, no grants here.</div>
</body></html>
"""

# Page with no recognisable structure → fallback path
GCF_TEXTONLY = """
<html><body>
  Greater Cincinnati Foundation accepts grant applications throughout
  the year. The next deadline is March 15, 2026. To apply, submit a
  Letter of Inquiry via our online system.
</body></html>
"""

# Minimal RSS feed
SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Foundation Feed</title>
    <item>
      <title>New Grant Opportunity</title>
      <link>https://example.org/grant-1</link>
      <description>Supporting youth literacy in OH</description>
      <pubDate>Wed, 20 May 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Operating Support Application</title>
      <link>https://example.org/grant-2</link>
      <description>For 501(c)(3) orgs in the tri-state.</description>
      <pubDate>Fri, 01 May 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Custom-parser smoke tests
# ─────────────────────────────────────────────────────────────────────────────

def test_gcf_parser_structured_pass():
    from tools.scrapers import gcf
    items = gcf.parse(GCF_HTML)
    titles = [it.title for it in items]
    assert "Safety Net Grant" in titles
    assert "Youth Opportunity Fund" in titles
    # Deadlines extracted
    safety = next(i for i in items if i.title == "Safety Net Grant")
    assert "March 15" in (safety.deadline or "")
    # URL resolved against base
    assert safety.url and safety.url.startswith("https://www.gcfdn.org")


def test_gcf_parser_fallback_text_scan():
    from tools.scrapers import gcf
    items = gcf.parse(GCF_TEXTONLY)
    # Fallback should return *something* keyword-matched
    assert items
    assert any("grant" in (it.summary or "").lower() for it in items)


def test_macarthur_parser_handles_empty_html():
    from tools.scrapers import macarthur
    assert macarthur.parse("") == []


def test_interact_for_health_parser_handles_malformed():
    from tools.scrapers import interact_for_health
    assert interact_for_health.parse("<not really html") in ([], list())


def test_parser_registry_has_three_entries():
    from tools.scrapers import PARSER_REGISTRY
    assert {"gcf", "macarthur", "interact_for_health"}.issubset(PARSER_REGISTRY.keys())


# ─────────────────────────────────────────────────────────────────────────────
# RSS parser
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_rss_extracts_items():
    from tools.source_monitor import parse_rss
    items = parse_rss(SAMPLE_RSS)
    assert len(items) == 2
    titles = {it["title"] for it in items}
    assert "New Grant Opportunity" in titles
    assert "Operating Support Application" in titles
    assert items[0]["link"] == "https://example.org/grant-1"


def test_parse_rss_handles_garbage():
    from tools.source_monitor import parse_rss
    assert parse_rss("")   == []
    assert parse_rss("<<") == []


# ─────────────────────────────────────────────────────────────────────────────
# check_source — wired via patched _fetch
# ─────────────────────────────────────────────────────────────────────────────

def _add_source(db, org_id, **overrides):
    from portal.models.source import MonitoredSource, SourceKind
    defaults = dict(
        org_id  = org_id,
        name    = "Test Source",
        url     = "https://example.org/test",
        kind    = SourceKind.PAGE,
        enabled = True,
    )
    defaults.update(overrides)
    row = MonitoredSource(**defaults)
    db.add(row)
    db.flush()
    return row


def test_check_source_page_kind_records_hash(db_session):
    """page kind → hash content; success on first check, unchanged on second."""
    from portal.models.source import CheckStatus, SourceKind
    from tools.source_monitor import check_source

    org = make_org(db_session, "fv", "Found Village")
    src = _add_source(db_session, org.id, kind=SourceKind.PAGE,
                       url="https://example.org/page")
    db_session.commit()

    with patch("tools.source_monitor._fetch", return_value="<html><body>Hello World</body></html>"):
        check1 = check_source(db_session, src)
        db_session.commit()
        check2 = check_source(db_session, src)
        db_session.commit()

    assert check1.status == CheckStatus.SUCCESS
    assert check1.content_hash
    assert check2.status == CheckStatus.UNCHANGED
    assert check2.content_hash == check1.content_hash
    # Source bookkeeping
    db_session.refresh(src)
    assert src.last_success_at is not None
    assert src.failure_count   == 0
    assert src.success_count   >= 2


def test_check_source_records_failure_on_network_error(db_session):
    from portal.models.source import CheckStatus, SourceKind
    from tools.source_monitor import check_source

    org = make_org(db_session, "fv", "Found Village")
    src = _add_source(db_session, org.id, kind=SourceKind.PAGE)
    db_session.commit()

    with patch("tools.source_monitor._fetch", side_effect=ConnectionError("upstream down")):
        check = check_source(db_session, src)
        db_session.commit()

    assert check.status == CheckStatus.FAILED
    assert "ConnectionError" in (check.message or "")
    db_session.refresh(src)
    assert src.failure_count == 1
    assert src.last_failure_at is not None


def test_check_source_custom_dispatches_to_parser(db_session):
    from portal.models.source import CheckStatus, SourceKind
    from tools.source_monitor import check_source

    org = make_org(db_session, "fv", "Found Village")
    src = _add_source(db_session, org.id, kind=SourceKind.CUSTOM,
                       parser_key="gcf",
                       url="https://www.gcfdn.org/grants/")
    db_session.commit()

    with patch("tools.source_monitor._fetch", return_value=GCF_HTML):
        check = check_source(db_session, src)
        db_session.commit()

    assert check.status      == CheckStatus.SUCCESS
    assert check.items_found >= 2


def test_check_source_custom_with_unknown_parser_fails_gracefully(db_session):
    from portal.models.source import CheckStatus, SourceKind
    from tools.source_monitor import check_source

    org = make_org(db_session, "fv", "Found Village")
    src = _add_source(db_session, org.id, kind=SourceKind.CUSTOM,
                       parser_key="not_a_real_parser")
    db_session.commit()

    with patch("tools.source_monitor._fetch", return_value="<html></html>"):
        check = check_source(db_session, src)
        db_session.commit()

    assert check.status == CheckStatus.FAILED
    assert "parser" in (check.message or "").lower()


# ─────────────────────────────────────────────────────────────────────────────
# Health label derivation
# ─────────────────────────────────────────────────────────────────────────────

def test_health_label_unknown_then_red_then_green(db_session):
    from portal.models.source import CheckStatus, MonitoredSource, SourceKind
    from tools.source_monitor import check_source

    org = make_org(db_session, "fv", "Found Village")
    src = _add_source(db_session, org.id, kind=SourceKind.PAGE)
    db_session.commit()
    assert src.derive_health_label() == "unknown"

    # First check fails → red (no success ever)
    with patch("tools.source_monitor._fetch", side_effect=ConnectionError("x")):
        check_source(db_session, src)
        db_session.commit()
    db_session.refresh(src)
    assert src.derive_health_label() == "red"

    # Then succeeds → counters reset, health becomes green
    with patch("tools.source_monitor._fetch", return_value="<html>OK</html>"):
        check_source(db_session, src)
        db_session.commit()
    db_session.refresh(src)
    assert src.derive_health_label() == "green"


# ─────────────────────────────────────────────────────────────────────────────
# Sources router
# ─────────────────────────────────────────────────────────────────────────────

def test_list_sources_includes_own_and_global(client, db_session):
    from portal.models.source import MonitoredSource, SourceKind

    org_a   = make_org(db_session, "a", "Org A")
    org_b   = make_org(db_session, "b", "Org B")
    admin_a, p = make_user(db_session, email="admin@a.org", org=org_a)

    db_session.add(MonitoredSource(org_id=org_a.id,  name="A-own",  url="https://x", kind=SourceKind.PAGE))
    db_session.add(MonitoredSource(org_id=None,      name="Global", url="https://y", kind=SourceKind.PAGE))
    db_session.add(MonitoredSource(org_id=org_b.id,  name="B-own",  url="https://z", kind=SourceKind.PAGE))
    db_session.commit()

    token = _login(client, admin_a.email, p)
    r = client.get("/sources/", headers=_auth(token))
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert "A-own"  in names
    assert "Global" in names
    assert "B-own"  not in names


def test_create_source_admin_only(client, db_session):
    org = make_org(db_session, "fv", "Found Village")
    plain, p = make_user(db_session, email="staff@fv.org", org=org, role="user")
    token = _login(client, plain.email, p)
    r = client.post("/sources/", headers=_auth(token),
                    json={"name": "Test", "url": "https://example.org",
                          "kind": "page"})
    assert r.status_code == 403


def test_create_source_validates_custom_parser_key(client, db_session):
    org = make_org(db_session, "fv", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)

    r = client.post("/sources/", headers=_auth(token), json={
        "name": "Test Source", "url": "https://example.org", "kind": "custom",
        "parser_key": "no_such_parser",
    })
    assert r.status_code == 400
    assert "parser_key" in r.json()["detail"].lower()


def test_create_and_update_and_delete_source(client, db_session):
    org = make_org(db_session, "fv", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)

    # Create
    r = client.post("/sources/", headers=_auth(token), json={
        "name": "Test", "url": "https://x", "kind": "page",
    })
    assert r.status_code == 201
    source_id = r.json()["id"]

    # Update enabled=False
    r = client.put(f"/sources/{source_id}", headers=_auth(token),
                   json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    # Delete
    r = client.delete(f"/sources/{source_id}", headers=_auth(token))
    assert r.status_code == 204

    # List no longer contains it
    r = client.get("/sources/", headers=_auth(token))
    assert all(s["id"] != source_id for s in r.json())


def test_cross_org_source_modification_returns_403(client, db_session):
    """An admin can't touch another org's source row."""
    from portal.models.source import MonitoredSource, SourceKind

    org_a   = make_org(db_session, "a", "Org A")
    org_b   = make_org(db_session, "b", "Org B")
    admin_a, p = make_user(db_session, email="admin@a.org", org=org_a)
    token = _login(client, admin_a.email, p)

    b_source = MonitoredSource(org_id=org_b.id, name="B-own",
                                url="https://z", kind=SourceKind.PAGE)
    db_session.add(b_source); db_session.commit()

    r = client.put(f"/sources/{b_source.id}", headers=_auth(token),
                   json={"enabled": False})
    assert r.status_code == 403


def test_manual_check_returns_202(client, db_session):
    from portal.models.source import MonitoredSource, SourceKind

    org = make_org(db_session, "fv", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = _login(client, admin.email, p)
    src = MonitoredSource(org_id=org.id, name="X",
                           url="https://x", kind=SourceKind.PAGE)
    db_session.add(src); db_session.commit()

    # Patch the BG entry point so it doesn't actually fire over the network.
    with patch("portal.routers.sources._check_in_background") as bg:
        r = client.post(f"/sources/{src.id}/check", headers=_auth(token))
    assert r.status_code == 202
    bg.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator: daily_source_check
# ─────────────────────────────────────────────────────────────────────────────

def test_source_check_job_iterates_enabled_only(client, db_session):
    """run_source_check_job runs every enabled source + logs one ScheduledRun
    per source. Disabled sources are skipped."""
    from agent import orchestrator
    from portal.models.scheduled_run import ScheduledRun
    from portal.models.source        import MonitoredSource, SourceKind

    org = make_org(db_session, "fv", "Found Village")
    enabled  = MonitoredSource(org_id=org.id, name="E", url="https://e",
                                kind=SourceKind.PAGE, enabled=True)
    disabled = MonitoredSource(org_id=org.id, name="D", url="https://d",
                                kind=SourceKind.PAGE, enabled=False)
    db_session.add_all([enabled, disabled])
    db_session.commit()

    with patch("agent.orchestrator.SessionLocal", return_value=db_session), \
         patch("tools.source_monitor._fetch", return_value="<html>ok</html>"):
        db_session.close = lambda: None
        orchestrator.run_source_check_job()

    runs = (
        db_session.query(ScheduledRun)
                  .filter(ScheduledRun.job_name == "daily_source_check")
                  .all()
    )
    assert len(runs) == 1
    assert runs[0].status.value == "success"


def test_source_check_job_in_job_dispatch():
    """The new job is wired into the manual-trigger map."""
    from agent.orchestrator import JOB_DISPATCH, JOB_SOURCE_CHECK
    assert JOB_SOURCE_CHECK in JOB_DISPATCH
