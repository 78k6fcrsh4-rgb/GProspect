"""
agent/grants_ingestion.py
-------------------------
Phase 3a — IRS 990 ingestion.

Two layers:

  1. parse_grants_from_xml(xml_bytes) -> ParsedFiling
     Pure function. Takes raw 990 e-file XML and returns the filing's
     header info + a list of GrantRecord dataclasses. Defensive against
     schema variations (the 990 / 990-PF XML schema has evolved over
     the years; we match by local element name regardless of namespace
     or schema version).

  2. ingest_for_org(db, org_id, client) -> IngestionResult
     For each FunderCandidate (status != dismissed) in the org's pool,
     locate the most recent 990 filing via IrsForm990Client, parse it,
     and upsert Funder + RecipientOrg + Grant rows. Idempotent on
     re-runs.

This is intentionally targeted: we only ingest data for funders the
discovery cycle has already surfaced for at least one org. Bulk-scoped
ingestion (everything in OH + IL) is a Phase 4 optimization.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from xml.etree import ElementTree as ET

from portal.models.funder_candidate import CandidateStatus, FunderCandidate
from portal.models.grant            import (
    upsert_funder,
    upsert_grant,
    upsert_recipient_org,
)
from tools.irs_990                  import IrsForm990Client, IndexRow

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Parser output types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GrantRecord:
    """One row from Schedule I / Part XV grants-paid."""
    recipient_name:    Optional[str]
    recipient_ein:     Optional[str]
    recipient_city:    Optional[str]
    recipient_state:   Optional[str]
    recipient_zipcode: Optional[str]
    amount:            Optional[int]
    purpose:           Optional[str]


@dataclass
class FilerHeader:
    ein:           Optional[str]
    name:          Optional[str]
    city:          Optional[str]
    state:         Optional[str]
    zipcode:       Optional[str]
    tax_period:    Optional[int]    # YYYYMM
    tax_year:      Optional[int]
    return_type:   Optional[str]    # "990PF" / "990" — best-effort


@dataclass
class ParsedFiling:
    header: FilerHeader
    grants: list[GrantRecord] = field(default_factory=list)


@dataclass
class IngestionResult:
    funders_indexed:    int = 0
    grants_inserted:    int = 0
    grants_refreshed:   int = 0
    filings_missing:    int = 0
    filings_failed:     int = 0
    notes: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# XML helpers — local-name lookups so we don't depend on namespace prefixes
# ─────────────────────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Strip the namespace from an ElementTree element tag."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_first_text(elem: ET.Element, *local_names: str) -> Optional[str]:
    """Return the first text content found for any of the given local names."""
    targets = set(local_names)
    for e in elem.iter():
        if _local(e.tag) in targets and (e.text or "").strip():
            return e.text.strip()
    return None


def _find_first_int(elem: ET.Element, *local_names: str) -> Optional[int]:
    """Coerce to int when possible."""
    val = _find_first_text(elem, *local_names)
    if val is None:
        return None
    try:
        return int(val.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _children_with_local(elem: ET.Element, local_name: str) -> list[ET.Element]:
    return [c for c in elem.iter() if _local(c.tag) == local_name]


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

# Element-name patterns for grant records — covers both 990 Schedule I
# and 990-PF Part XV / Statement of Grants Paid across multiple schema
# generations.
GRANT_RECORD_TAGS = {
    # Most common across modern 990-PF schemas
    "GrantOrContributionPdDuringYrGrp",
    "GrantsContributionsPdDuringYrGrp",
    "GrantOrContributionPdDuringYrInd",   # individual grants
    # Schedule I on 990 public charities
    "RecipientTable",
    "GrantsOtherAsstToIndivInUSGrp",
    # Older / variant names
    "GrantsRecipientGrp",
    "RecipientGrp",
    "GrantsContributionsPaidDuringYrCash",
}


def parse_grants_from_xml(xml_bytes: bytes) -> ParsedFiling:
    """
    Parse one 990 e-file XML and extract the header + grant records.

    Returns ParsedFiling with possibly-empty grants list. Never raises
    on schema variations — best-effort extraction.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log.warning("XML parse failed: %s", e)
        return ParsedFiling(header=FilerHeader(
            ein=None, name=None, city=None, state=None, zipcode=None,
            tax_period=None, tax_year=None, return_type=None,
        ))

    header = _parse_header(root)
    grants: list[GrantRecord] = []

    for elem in root.iter():
        if _local(elem.tag) in GRANT_RECORD_TAGS:
            rec = _parse_grant_record(elem)
            if rec.recipient_name or rec.amount:
                grants.append(rec)

    return ParsedFiling(header=header, grants=grants)


def _parse_header(root: ET.Element) -> FilerHeader:
    """Pull ReturnHeader info — Filer EIN, name, address, tax period."""
    ein  = _find_first_text(root, "EIN", "Ein", "FilerEIN")
    name = _find_first_text(
        root, "BusinessNameLine1Txt", "BusinessName", "FilerBusinessName",
    )
    # The Filer address typically lives under <Filer><USAddress>. Restrict
    # to USAddress nodes that sit inside an element whose name contains
    # "Filer" if possible, otherwise pick the first US address.
    filer_addr = None
    for e in root.iter():
        if _local(e.tag) in ("Filer", "FilerGrp"):
            for sub in e.iter():
                if _local(sub.tag) in ("USAddress", "USAddressGrp"):
                    filer_addr = sub
                    break
        if filer_addr is not None:
            break
    if filer_addr is None:
        for e in root.iter():
            if _local(e.tag) in ("USAddress", "USAddressGrp"):
                filer_addr = e
                break

    city  = _find_first_text(filer_addr, "CityNm", "City") if filer_addr is not None else None
    state = _find_first_text(filer_addr, "StateAbbreviationCd", "State") if filer_addr is not None else None
    zipc  = _find_first_text(filer_addr, "ZIPCd", "Zip") if filer_addr is not None else None

    # Tax period
    tax_end = _find_first_text(root, "TaxPeriodEndDt", "TaxPeriodEnd", "TaxPeriodEndDate")
    tax_period = None
    if tax_end:
        m = re.match(r"^(\d{4})-(\d{2})", tax_end)
        if m:
            tax_period = int(m.group(1)) * 100 + int(m.group(2))

    # Return type
    return_type = None
    for e in root.iter():
        ln = _local(e.tag)
        if ln in ("IRS990PF",):
            return_type = "990PF"
            break
        if ln in ("IRS990",):
            return_type = "990"
            break

    return FilerHeader(
        ein         = ein,
        name        = name,
        city        = city,
        state       = state,
        zipcode     = zipc,
        tax_period  = tax_period,
        tax_year    = (tax_period // 100) if tax_period else None,
        return_type = return_type,
    )


def _parse_grant_record(elem: ET.Element) -> GrantRecord:
    """Pull one grant record from a Recipient/Grant group element."""
    name = _find_first_text(
        elem,
        # 990-PF business name
        "RecipientBusinessName",
        "BusinessNameLine1Txt",
        "BusinessName",
        # 990-PF / 990 person name
        "RecipientPersonNm",
        "PersonNm",
        # Other variants
        "Recipient",
        "Name",
    )
    ein = _find_first_text(elem, "RecipientEIN", "RecipientEin")

    # Address blocks vary in name; look for child US address group
    addr_city = None
    addr_state = None
    addr_zip = None
    for sub in elem.iter():
        if _local(sub.tag) in ("USAddress", "USAddressGrp",
                                "RecipientUSAddress", "RecipientAddress"):
            addr_city  = _find_first_text(sub, "CityNm", "City")
            addr_state = _find_first_text(sub, "StateAbbreviationCd", "State")
            addr_zip   = _find_first_text(sub, "ZIPCd", "Zip")
            break

    amount = _find_first_int(
        elem,
        "CashGrantAmt", "Amt", "GrantAmt", "Amount", "AmountOfGrantPaymentAmt",
    )
    purpose = _find_first_text(
        elem,
        "PurposeOfGrantTxt", "Purpose", "PurposeOfGrant",
        "PurposeOfGrantOrContributionTxt",
    )

    return GrantRecord(
        recipient_name    = (name or "").strip() or None,
        recipient_ein     = ein.strip() if ein else None,
        recipient_city    = (addr_city or "").strip() or None,
        recipient_state   = (addr_state or "").strip() or None,
        recipient_zipcode = (addr_zip or "").strip() or None,
        amount            = amount,
        purpose           = purpose,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion job — targeted to candidate EINs for one org
# ─────────────────────────────────────────────────────────────────────────────

def ingest_for_org(
    *,
    db,
    org_id:        int,
    client:        Optional[IrsForm990Client] = None,
    years_back:    int                        = 3,
    max_per_run:   int                        = 30,
) -> IngestionResult:
    """
    For each non-dismissed FunderCandidate in the org's pool, locate the
    most recent 990 filing on file with the IRS and ingest its grants.

    Args:
        db:           SQLAlchemy session — caller commits.
        org_id:       Organization scoping the candidate pool.
        client:       IrsForm990Client (tests inject a stub).
        years_back:   How many years of IRS index files to search.
        max_per_run:  Cap on funders processed in one run.

    Returns: IngestionResult with counts + notes.
    """
    client = client or IrsForm990Client()
    result = IngestionResult()

    candidates = (
        db.query(FunderCandidate)
          .filter(FunderCandidate.org_id == org_id,
                  FunderCandidate.status != CandidateStatus.DISMISSED)
          .order_by(FunderCandidate.score.desc())
          .limit(max_per_run)
          .all()
    )
    if not candidates:
        result.notes.append("No active candidates to ingest for.")
        return result

    for cand in candidates:
        # Find latest filing
        try:
            filing = client.find_latest_filing(
                ein         = cand.ein,
                years_back  = years_back,
            )
        except Exception as e:
            log.warning("Index lookup failed for EIN=%s: %s", cand.ein, e)
            result.filings_failed += 1
            continue

        if filing is None:
            result.filings_missing += 1
            continue

        try:
            xml_bytes = client.get_filing_xml(filing.object_id)
        except Exception as e:
            log.warning("Filing download failed for object_id=%s: %s",
                        filing.object_id, e)
            result.filings_failed += 1
            continue

        parsed = parse_grants_from_xml(xml_bytes)

        # Upsert Funder
        funder_row, _ = upsert_funder(
            db,
            ein       = cand.ein,
            name      = parsed.header.name or cand.funder_name,
            city      = parsed.header.city  or cand.funder_city,
            state     = parsed.header.state or cand.funder_state,
            zipcode   = parsed.header.zipcode or cand.funder_zipcode,
            ntee_code = cand.ntee_code,
            formtype  = _formtype_from_return_type(parsed.header.return_type),
        )
        funder_row.last_990pf_year  = parsed.header.tax_year
        from datetime import datetime, timezone as _tz
        funder_row.last_ingested_at = datetime.now(_tz.utc)
        result.funders_indexed += 1

        # Upsert each grant
        for g in parsed.grants:
            if not g.recipient_name:
                continue
            rec_row, _ = upsert_recipient_org(
                db,
                name    = g.recipient_name,
                ein     = g.recipient_ein,
                city    = g.recipient_city,
                state   = g.recipient_state,
                zipcode = g.recipient_zipcode,
            )
            _, inserted = upsert_grant(
                db,
                funder_id               = funder_row.id,
                recipient_id            = rec_row.id,
                fiscal_year             = parsed.header.tax_year,
                tax_period              = parsed.header.tax_period,
                amount                  = g.amount,
                purpose                 = g.purpose,
                source_filing_object_id = filing.object_id,
            )
            if inserted:
                result.grants_inserted += 1
            else:
                result.grants_refreshed += 1

        # Refresh aggregate stats on the Funder row
        total_count, total_amount = _grant_stats_for_funder(db, funder_row.id)
        funder_row.total_grants_indexed = total_count
        funder_row.total_amount_indexed = total_amount

    result.notes.append(
        f"Processed {len(candidates)} candidate(s) — "
        f"{result.grants_inserted} new grants, "
        f"{result.grants_refreshed} refreshed."
    )
    return result


def _formtype_from_return_type(return_type: Optional[str]) -> Optional[int]:
    if return_type == "990":   return 0
    if return_type == "990EZ": return 1
    if return_type == "990PF": return 2
    return None


def _grant_stats_for_funder(db, funder_id: int) -> tuple[int, int]:
    """Return (count, total_amount) for the funder's grants."""
    from sqlalchemy import func
    from portal.models.grant import Grant
    row = (
        db.query(
            func.count(Grant.id),
            func.coalesce(func.sum(Grant.amount), 0),
        )
        .filter(Grant.funder_id == funder_id)
        .one()
    )
    return int(row[0] or 0), int(row[1] or 0)
