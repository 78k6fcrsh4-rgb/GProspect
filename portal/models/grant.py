"""
portal/models/grant.py
----------------------
Phase 3a — the grant-ingestion data model.

Three new tables that hold *globally ingested* 990-PF data (not per-org).
This is the substrate Phase 3b uses for peer-grant analysis and warm-path
inference.

  Funder
    Master record per foundation EIN. Different from FunderCandidate
    (which is per-org). Funder rows hold the canonical name, location,
    and aggregate ingestion stats.

  RecipientOrg
    Organizations that received grants in the ingested filings. Often
    only a name + address is available (no EIN), so we maintain a
    normalized_name for fuzzy joining with peer-org clusters later.

  Grant
    One row per (funder, recipient, fiscal year). Idempotent on the
    composite key (funder_id, recipient_id, fiscal_year,
    source_filing_object_id) so re-ingesting the same filing doesn't
    duplicate.

This data is intentionally global (not tenanted by org_id) — it's
public IRS data and many orgs benefit from sharing the cache. Privacy
review: no PII beyond what's already on a public 990.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
)
from sqlalchemy.orm import relationship

from database.db import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Funder — master record per EIN
# ─────────────────────────────────────────────────────────────────────────────

class Funder(Base):
    """
    A foundation (or other grantmaker) whose 990 data we've ingested.

    Identified by EIN. Different from FunderCandidate (per-org, scored,
    statused) — this is the canonical global record.
    """

    __tablename__  = "funders"
    __table_args__ = (
        UniqueConstraint("ein", name="uq_funders_ein"),
    )

    id   = Column(Integer, primary_key=True, index=True)
    ein  = Column(String(11), nullable=False, index=True)   # "XX-XXXXXXX"
    name = Column(String, nullable=False)

    # Identity / classification
    city        = Column(String, nullable=True)
    state       = Column(String(2), nullable=True)
    zipcode     = Column(String, nullable=True)
    ntee_code   = Column(String, nullable=True)
    formtype    = Column(Integer, nullable=True)   # last filing's form type

    # Ingestion bookkeeping
    last_990pf_year      = Column(Integer, nullable=True)
    last_ingested_at     = Column(DateTime(timezone=True), nullable=True)
    total_grants_indexed = Column(Integer, nullable=False, default=0)
    total_amount_indexed = Column(Integer, nullable=False, default=0)  # USD

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow,
                        onupdate=_utcnow, nullable=False)

    grants_given = relationship("Grant", back_populates="funder",
                                cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "ein":                  self.ein,
            "name":                 self.name,
            "city":                 self.city,
            "state":                self.state,
            "zipcode":              self.zipcode,
            "ntee_code":            self.ntee_code,
            "formtype":             self.formtype,
            "last_990pf_year":      self.last_990pf_year,
            "last_ingested_at":     self.last_ingested_at.isoformat() if self.last_ingested_at else None,
            "total_grants_indexed": self.total_grants_indexed,
            "total_amount_indexed": self.total_amount_indexed,
        }


# ─────────────────────────────────────────────────────────────────────────────
# RecipientOrg — global, possibly without an EIN
# ─────────────────────────────────────────────────────────────────────────────

class RecipientOrg(Base):
    """
    An organization that received a grant in one of the ingested filings.

    Schedule I lists recipients by name + address — the EIN is sometimes
    present, often not. We store both, plus a normalized_name for the
    fuzzy-matching joins Phase 3b will use to identify peer orgs.

    Unique-ish constraint: (normalized_name, state). Two distinct orgs
    with the same name in different states are kept separate; same name
    + same state is treated as the same recipient. EIN, when present,
    overrides this and creates a separate identity.
    """

    __tablename__  = "recipient_orgs"
    __table_args__ = (
        UniqueConstraint("normalized_name", "state", "ein",
                         name="uq_recipient_orgs_name_state_ein"),
    )

    id              = Column(Integer, primary_key=True, index=True)
    ein             = Column(String(11), nullable=True, index=True)
    name            = Column(String, nullable=False)
    normalized_name = Column(String, nullable=False, index=True)
    city            = Column(String, nullable=True)
    state           = Column(String(2), nullable=True, index=True)
    zipcode         = Column(String, nullable=True)

    # NTEE inferred from related ingestion data — null until we figure it out
    ntee_code = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow,
                        onupdate=_utcnow, nullable=False)

    grants_received = relationship("Grant", back_populates="recipient",
                                   cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id":              self.id,
            "ein":             self.ein,
            "name":            self.name,
            "normalized_name": self.normalized_name,
            "city":            self.city,
            "state":           self.state,
            "zipcode":         self.zipcode,
            "ntee_code":       self.ntee_code,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Grant — one funder → recipient transaction in one fiscal year
# ─────────────────────────────────────────────────────────────────────────────

class Grant(Base):
    """
    A single grant from a Funder to a RecipientOrg.

    Identity: (funder_id, recipient_id, fiscal_year,
               source_filing_object_id). Re-ingesting the same filing
    is a no-op rather than producing duplicates.

    Amount stored as integer USD (no cents — 990 reporting is rounded).
    """

    __tablename__  = "grants"
    __table_args__ = (
        UniqueConstraint(
            "funder_id", "recipient_id", "fiscal_year", "source_filing_object_id",
            name = "uq_grants_unique_record",
        ),
    )

    id           = Column(Integer, primary_key=True, index=True)
    funder_id    = Column(Integer, ForeignKey("funders.id",        ondelete="CASCADE"),
                          nullable=False, index=True)
    recipient_id = Column(Integer, ForeignKey("recipient_orgs.id", ondelete="CASCADE"),
                          nullable=False, index=True)

    amount       = Column(Integer, nullable=True)        # USD
    fiscal_year  = Column(Integer, nullable=True, index=True)
    tax_period   = Column(Integer, nullable=True)        # YYYYMM
    purpose      = Column(Text, nullable=True)

    source_filing_object_id = Column(String, nullable=True)
    created_at              = Column(DateTime(timezone=True),
                                     default=_utcnow, nullable=False)

    funder    = relationship("Funder",       back_populates="grants_given")
    recipient = relationship("RecipientOrg", back_populates="grants_received")

    def to_dict(self) -> dict:
        return {
            "id":                       self.id,
            "funder_id":                self.funder_id,
            "recipient_id":             self.recipient_id,
            "amount":                   self.amount,
            "fiscal_year":              self.fiscal_year,
            "tax_period":               self.tax_period,
            "purpose":                  self.purpose,
            "source_filing_object_id":  self.source_filing_object_id,
            "created_at":               self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

import re as _re


def normalize_name(name: str | None) -> str:
    """
    Stable name normalization for fuzzy joining.

      - lowercase
      - strip apostrophes (so "Children's" → "childrens", not "children s")
      - replace other punctuation with whitespace
      - drop leading "the"
      - drop common corporate suffixes (Inc, LLC, Corp, Corporation,
        Company, Co)
      - collapse whitespace

    NOTE: "Foundation" is intentionally kept — it's part of the name we
    want to match against, not noise.
    """
    if not name:
        return ""
    s = name.strip().lower()
    # Apostrophes vanish so possessives don't introduce a word boundary.
    s = _re.sub(r"['’]",                  "", s)
    # Other punctuation becomes whitespace.
    s = _re.sub(r"[^a-z0-9\s]",                 " ", s)
    # Drop a leading "the" and any standalone corporate suffix.
    s = _re.sub(r"\bthe\b",                     "", s)
    s = _re.sub(r"\b(inc|incorporated|llc|corp|corporation|company|co)\b", "", s)
    s = _re.sub(r"\s+",                         " ", s).strip()
    return s


def upsert_funder(
    db,
    *,
    ein:       str,
    name:      str,
    city:      str | None = None,
    state:     str | None = None,
    zipcode:   str | None = None,
    ntee_code: str | None = None,
    formtype:  int | None = None,
) -> tuple[Funder, bool]:
    """
    Idempotent: returns (row, was_inserted).
    Refreshes name/location/ntee/formtype on existing rows; does NOT touch
    ingestion stats (the ingester updates those after grants are persisted).
    """
    existing = db.query(Funder).filter(Funder.ein == ein).one_or_none()
    if existing is None:
        row = Funder(
            ein       = ein,
            name      = name,
            city      = city,
            state     = state,
            zipcode   = zipcode,
            ntee_code = ntee_code,
            formtype  = formtype,
        )
        db.add(row)
        db.flush()
        return row, True
    existing.name = name or existing.name
    if city      is not None: existing.city      = city
    if state     is not None: existing.state     = state
    if zipcode   is not None: existing.zipcode   = zipcode
    if ntee_code is not None: existing.ntee_code = ntee_code
    if formtype  is not None: existing.formtype  = formtype
    db.flush()
    return existing, False


def upsert_recipient_org(
    db,
    *,
    name:    str,
    ein:     str | None = None,
    city:    str | None = None,
    state:   str | None = None,
    zipcode: str | None = None,
) -> tuple[RecipientOrg, bool]:
    """
    Idempotent: matches on (normalized_name, state, ein). Returns
    (row, was_inserted).
    """
    norm = normalize_name(name)
    q = db.query(RecipientOrg).filter(
        RecipientOrg.normalized_name == norm,
        RecipientOrg.state           == state,
    )
    if ein:
        q = q.filter(RecipientOrg.ein == ein)
    else:
        q = q.filter(RecipientOrg.ein.is_(None))

    existing = q.one_or_none()
    if existing is None:
        row = RecipientOrg(
            name            = name,
            normalized_name = norm,
            ein             = ein,
            city            = city,
            state           = state,
            zipcode         = zipcode,
        )
        db.add(row)
        db.flush()
        return row, True

    if city    and not existing.city:    existing.city    = city
    if zipcode and not existing.zipcode: existing.zipcode = zipcode
    db.flush()
    return existing, False


def upsert_grant(
    db,
    *,
    funder_id:               int,
    recipient_id:            int,
    fiscal_year:             int | None,
    tax_period:              int | None,
    amount:                  int | None,
    purpose:                 str | None,
    source_filing_object_id: str | None,
) -> tuple[Grant, bool]:
    """
    Idempotent on the unique constraint. Returns (row, was_inserted).
    """
    q = db.query(Grant).filter(
        Grant.funder_id               == funder_id,
        Grant.recipient_id            == recipient_id,
        Grant.fiscal_year             == fiscal_year,
        Grant.source_filing_object_id == source_filing_object_id,
    )
    existing = q.one_or_none()
    if existing is None:
        row = Grant(
            funder_id               = funder_id,
            recipient_id            = recipient_id,
            fiscal_year             = fiscal_year,
            tax_period              = tax_period,
            amount                  = amount,
            purpose                 = purpose,
            source_filing_object_id = source_filing_object_id,
        )
        db.add(row)
        db.flush()
        return row, True

    # Refresh updateable fields only
    if amount  is not None: existing.amount  = amount
    if purpose is not None: existing.purpose = purpose
    if tax_period is not None: existing.tax_period = tax_period
    db.flush()
    return existing, False
