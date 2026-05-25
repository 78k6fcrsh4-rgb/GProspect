"""
tests/test_phase3a_grants.py
----------------------------
Phase 3a acceptance — IRS 990 XML parser + ingestion job + endpoints.

The IRS S3 fetches are stubbed via a FakeIrsClient so tests don't hit
the network. Schedule I parsing uses inline XML fixtures covering both
the 990-PF and 990 schema variants we expect in the wild.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.conftest import make_org, make_user


# ─────────────────────────────────────────────────────────────────────────────
# Inline XML fixtures
# ─────────────────────────────────────────────────────────────────────────────

# A minimal 990-PF with two grants in the modern schema. Uses the
# `efile.irs.gov/efile` namespace prefix the real IRS files use.
PF_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Return xmlns="http://www.irs.gov/efile">
  <ReturnHeader>
    <TaxPeriodEndDt>2023-12-31</TaxPeriodEndDt>
    <Filer>
      <EIN>311234567</EIN>
      <BusinessName>
        <BusinessNameLine1Txt>Test Foundation</BusinessNameLine1Txt>
      </BusinessName>
      <USAddress>
        <AddressLine1Txt>1 Main St</AddressLine1Txt>
        <CityNm>Cincinnati</CityNm>
        <StateAbbreviationCd>OH</StateAbbreviationCd>
        <ZIPCd>45202</ZIPCd>
      </USAddress>
    </Filer>
  </ReturnHeader>
  <ReturnData>
    <IRS990PF>
      <SupplementaryInformationGrp>
        <GrantOrContributionPdDuringYrGrp>
          <RecipientBusinessName>
            <BusinessNameLine1Txt>Found Village</BusinessNameLine1Txt>
          </RecipientBusinessName>
          <RecipientUSAddress>
            <CityNm>Cincinnati</CityNm>
            <StateAbbreviationCd>OH</StateAbbreviationCd>
            <ZIPCd>45206</ZIPCd>
          </RecipientUSAddress>
          <RecipientEIN>820123456</RecipientEIN>
          <CashGrantAmt>50000</CashGrantAmt>
          <PurposeOfGrantTxt>Operating support</PurposeOfGrantTxt>
        </GrantOrContributionPdDuringYrGrp>
        <GrantOrContributionPdDuringYrGrp>
          <RecipientBusinessName>
            <BusinessNameLine1Txt>Other Cincinnati Nonprofit</BusinessNameLine1Txt>
          </RecipientBusinessName>
          <RecipientUSAddress>
            <CityNm>Cincinnati</CityNm>
            <StateAbbreviationCd>OH</StateAbbreviationCd>
          </RecipientUSAddress>
          <CashGrantAmt>25000</CashGrantAmt>
          <PurposeOfGrantTxt>Youth services program</PurposeOfGrantTxt>
        </GrantOrContributionPdDuringYrGrp>
      </SupplementaryInformationGrp>
    </IRS990PF>
  </ReturnData>
</Return>
"""

# A minimal 990 (public charity) Schedule I with one grant.
PUB_990_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Return xmlns="http://www.irs.gov/efile">
  <ReturnHeader>
    <TaxPeriodEndDt>2024-06-30</TaxPeriodEndDt>
    <Filer>
      <EIN>311111111</EIN>
      <BusinessName>
        <BusinessNameLine1Txt>Sample Public Charity</BusinessNameLine1Txt>
      </BusinessName>
      <USAddress>
        <CityNm>Columbus</CityNm>
        <StateAbbreviationCd>OH</StateAbbreviationCd>
      </USAddress>
    </Filer>
  </ReturnHeader>
  <ReturnData>
    <IRS990>
      <IRS990ScheduleI>
        <RecipientTable>
          <RecipientBusinessName>
            <BusinessNameLine1Txt>Found Village</BusinessNameLine1Txt>
          </RecipientBusinessName>
          <RecipientEIN>820123456</RecipientEIN>
          <CashGrantAmt>15000</CashGrantAmt>
          <PurposeOfGrantTxt>Program support</PurposeOfGrantTxt>
        </RecipientTable>
      </IRS990ScheduleI>
    </IRS990>
  </ReturnData>
</Return>
"""

# An empty / malformed XML — should not crash the parser.
BAD_XML = b"<not really valid"


# ─────────────────────────────────────────────────────────────────────────────
# Parser tests
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_990pf_xml():
    from agent.grants_ingestion import parse_grants_from_xml
    p = parse_grants_from_xml(PF_XML)
    assert p.header.ein == "311234567"
    assert p.header.name == "Test Foundation"
    assert p.header.state == "OH"
    assert p.header.tax_period == 202312
    assert p.header.tax_year   == 2023
    assert p.header.return_type == "990PF"

    assert len(p.grants) == 2
    g1, g2 = p.grants
    assert g1.recipient_name  == "Found Village"
    assert g1.recipient_ein   == "820123456"
    assert g1.recipient_state == "OH"
    assert g1.amount          == 50000
    assert g1.purpose         == "Operating support"
    assert g2.recipient_name  == "Other Cincinnati Nonprofit"
    assert g2.recipient_ein   is None
    assert g2.amount          == 25000


def test_parse_990_xml_schedule_i():
    from agent.grants_ingestion import parse_grants_from_xml
    p = parse_grants_from_xml(PUB_990_XML)
    assert p.header.return_type == "990"
    assert p.header.tax_year    == 2024
    assert len(p.grants) == 1
    assert p.grants[0].recipient_name == "Found Village"
    assert p.grants[0].amount == 15000


def test_parse_bad_xml_returns_empty():
    from agent.grants_ingestion import parse_grants_from_xml
    p = parse_grants_from_xml(BAD_XML)
    assert p.grants == []
    assert p.header.ein is None


# ─────────────────────────────────────────────────────────────────────────────
# Normalize-name helper
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_name_stable_across_variants():
    from portal.models.grant import normalize_name
    assert normalize_name("The MacArthur Foundation, Inc.") == "macarthur foundation"
    assert normalize_name("MACARTHUR  FOUNDATION INC")      == "macarthur foundation"
    assert normalize_name("Children's Defense Fund, LLC")   == "childrens defense fund"
    assert normalize_name("")                               == ""
    assert normalize_name(None)                             == ""


# ─────────────────────────────────────────────────────────────────────────────
# Fake IRS client + ingestion integration
# ─────────────────────────────────────────────────────────────────────────────

class _FakeIndexRow:
    def __init__(self, ein, return_type, tax_period, object_id, taxpayer_name=""):
        self.ein           = ein.rjust(9, "0")
        self.return_type   = return_type
        self.tax_period    = tax_period
        self.object_id     = object_id
        self.taxpayer_name = taxpayer_name
        # Unused fields
        self.return_id     = ""
        self.filing_type   = ""
        self.sub_date      = ""
        self.dln           = ""


class FakeIrsClient:
    """
    Returns canned IndexRow + XML responses keyed by EIN. Lets ingestion
    tests run without hitting the network.
    """
    def __init__(self, index_by_ein: dict[str, _FakeIndexRow],
                 xml_by_object: dict[str, bytes]):
        self.index_by_ein  = index_by_ein
        self.xml_by_object = xml_by_object
        self.calls         = {"index": 0, "xml": 0}

    def find_latest_filing(self, *, ein, years_back=3, return_types=None,
                            current_year=None):
        from tools.irs_990 import _normalize_ein_digits
        self.calls["index"] += 1
        norm = _normalize_ein_digits(ein)
        return self.index_by_ein.get(norm)

    def find_recent_filings(self, *, ein, **_):
        latest = self.find_latest_filing(ein=ein)
        return [latest] if latest else []

    def get_filing_xml(self, object_id: str):
        self.calls["xml"] += 1
        if object_id not in self.xml_by_object:
            raise FileNotFoundError(object_id)
        return self.xml_by_object[object_id]

    def get_index_year(self, year):
        return list(self.index_by_ein.values())


def _make_funder_candidate(db, *, org_id, ein, name="Foo Foundation"):
    from portal.models.funder_candidate import FunderCandidate, CandidateStatus
    fc = FunderCandidate(
        org_id       = org_id,
        ein          = ein,
        funder_name  = name,
        score        = 4.0,
        rationale    = "test",
        signals      = {},
        status       = CandidateStatus.CANDIDATE,
        funder_state = "OH",
    )
    db.add(fc)
    db.flush()
    return fc


@pytest.fixture()
def fake_irs():
    return FakeIrsClient(
        index_by_ein = {
            "311234567": _FakeIndexRow("311234567", "990PF",  202312, "obj_pf_1"),
            "311111111": _FakeIndexRow("311111111", "990",    202406, "obj_990_1"),
            "999999999": _FakeIndexRow("999999999", "990PF",  202312, "obj_missing"),
        },
        xml_by_object = {
            "obj_pf_1":  PF_XML,
            "obj_990_1": PUB_990_XML,
            # obj_missing intentionally omitted — get_filing_xml raises
        },
    )


def test_ingest_for_org_creates_funder_and_grants(client, db_session, fake_irs):
    from agent.grants_ingestion       import ingest_for_org
    from portal.models.grant          import Funder, Grant, RecipientOrg

    org = make_org(db_session, "found-village", "Found Village")
    make_user(db_session, email="admin@fv.org", org=org)
    _make_funder_candidate(db_session, org_id=org.id, ein="31-1234567",
                           name="Test Foundation")
    db_session.commit()

    result = ingest_for_org(db=db_session, org_id=org.id, client=fake_irs)
    db_session.commit()

    assert result.funders_indexed   == 1
    assert result.grants_inserted   == 2
    assert result.grants_refreshed  == 0
    assert result.filings_missing   == 0
    assert result.filings_failed    == 0

    # Funder + RecipientOrg + Grant rows persisted
    f = db_session.query(Funder).filter_by(ein="31-1234567").one()
    assert f.total_grants_indexed == 2
    assert f.total_amount_indexed == 75_000
    assert f.last_990pf_year      == 2023

    recipients = db_session.query(RecipientOrg).all()
    assert len(recipients) == 2
    assert {r.name for r in recipients} == {"Found Village", "Other Cincinnati Nonprofit"}

    grants = db_session.query(Grant).all()
    assert len(grants) == 2


def test_ingest_idempotent_across_runs(client, db_session, fake_irs):
    """Re-running ingestion must refresh, not duplicate."""
    from agent.grants_ingestion import ingest_for_org
    from portal.models.grant    import Funder, Grant

    org = make_org(db_session, "found-village", "Found Village")
    make_user(db_session, email="admin@fv.org", org=org)
    _make_funder_candidate(db_session, org_id=org.id, ein="31-1234567")
    db_session.commit()

    r1 = ingest_for_org(db=db_session, org_id=org.id, client=fake_irs)
    db_session.commit()
    r2 = ingest_for_org(db=db_session, org_id=org.id, client=fake_irs)
    db_session.commit()

    assert r2.grants_inserted  == 0
    assert r2.grants_refreshed == 2

    # Still exactly 1 Funder + 2 Grants — no duplicates
    assert db_session.query(Funder).count() == 1
    assert db_session.query(Grant).count()  == 2


def test_ingest_skips_missing_filing(client, db_session, fake_irs):
    """A candidate with no filing matched in the index should be counted."""
    from agent.grants_ingestion import ingest_for_org

    org = make_org(db_session, "found-village", "Found Village")
    make_user(db_session, email="admin@fv.org", org=org)
    _make_funder_candidate(db_session, org_id=org.id, ein="42-0000000")
    db_session.commit()

    result = ingest_for_org(db=db_session, org_id=org.id, client=fake_irs)
    assert result.filings_missing == 1
    assert result.funders_indexed == 0


def test_ingest_skips_failed_download(client, db_session, fake_irs):
    """A candidate whose XML download fails should be counted as failed."""
    from agent.grants_ingestion import ingest_for_org

    org = make_org(db_session, "found-village", "Found Village")
    make_user(db_session, email="admin@fv.org", org=org)
    _make_funder_candidate(db_session, org_id=org.id, ein="99-9999999")
    db_session.commit()

    result = ingest_for_org(db=db_session, org_id=org.id, client=fake_irs)
    assert result.filings_failed == 1
    assert result.funders_indexed == 0


# ─────────────────────────────────────────────────────────────────────────────
# Index CSV parser
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_index_csv():
    from tools.irs_990 import _parse_index_csv
    csv_text = (
        "RETURN_ID,FILING_TYPE,EIN,TAX_PERIOD,SUB_DATE,TAXPAYER_NAME,RETURN_TYPE,DLN,OBJECT_ID\n"
        "1,EFILE,311234567,202312,2024-05-15,Test Foundation,990PF,123,obj_a\n"
        "2,EFILE,311111111,202406,2024-09-15,Sample Charity,990,456,obj_b\n"
        ",,,bogus,,,,,\n"     # malformed row — should be skipped
    )
    rows = _parse_index_csv(csv_text)
    assert len(rows) == 2
    assert rows[0].ein == "311234567"
    assert rows[0].return_type == "990PF"
    assert rows[1].ein == "311111111"


# ─────────────────────────────────────────────────────────────────────────────
# Router endpoints
# ─────────────────────────────────────────────────────────────────────────────

def test_grants_ingest_returns_202(client, db_session, fake_irs):
    """
    POST /grants/ingest returns 202 and dispatches the BG worker.

    We patch the background entry point so the worker doesn't run against
    the production SessionLocal (which points at sqlite:///./grant_prospector.db,
    not the test engine) — we're only verifying the route's response here.
    The actual worker behavior is exercised end-to-end by
    test_ingest_for_org_creates_funder_and_grants.
    """
    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    token = client.post("/auth/login",
                        data={"username": admin.email, "password": p}).json()["access_token"]

    with patch("portal.routers.grants._ingest_in_background") as bg:
        r = client.post("/grants/ingest",
                        headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 202
    assert "queued" in r.json().get("message", "").lower()
    # BackgroundTasks runs the task as part of the response lifecycle in
    # TestClient — confirm our patched stub was the thing it called.
    bg.assert_called_once()


def test_grants_ingest_admin_only(client, db_session):
    org = make_org(db_session, "found-village", "Found Village")
    plain, p = make_user(db_session, email="staff@fv.org", org=org, role="user")
    token = client.post("/auth/login",
                        data={"username": plain.email, "password": p}).json()["access_token"]

    r = client.post("/grants/ingest", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_grants_status_lists_candidates(client, db_session, fake_irs):
    """/grants/status returns one row per candidate, ingested or not."""
    from agent.grants_ingestion import ingest_for_org

    org = make_org(db_session, "found-village", "Found Village")
    admin, p = make_user(db_session, email="admin@fv.org", org=org)
    _make_funder_candidate(db_session, org_id=org.id, ein="31-1234567",
                           name="Test Foundation")
    _make_funder_candidate(db_session, org_id=org.id, ein="42-0000000",
                           name="Not Yet Ingested Foundation")
    db_session.commit()

    # Run ingestion so one candidate has data, the other doesn't
    ingest_for_org(db=db_session, org_id=org.id, client=fake_irs)
    db_session.commit()

    token = client.post("/auth/login",
                        data={"username": admin.email, "password": p}).json()["access_token"]
    r = client.get("/grants/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2

    by_ein = {row["ein"]: row for row in body}
    assert by_ein["31-1234567"]["ingested"]            is True
    assert by_ein["31-1234567"]["total_grants_indexed"] == 2
    assert by_ein["42-0000000"]["ingested"]            is False
