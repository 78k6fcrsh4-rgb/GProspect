"""
tools/irs_990.py
----------------
Client for the IRS public 990 e-file bulk data.

The IRS publishes processed Form 990 returns as XML files in a public
S3 bucket. We use HTTPS (no AWS SDK / no credentials needed):

  Base:           https://s3.amazonaws.com/irs-form-990/
  Yearly index:   index_<YYYY>.csv
  Filing XML:     <OBJECT_ID>_public.xml

Index CSV columns: RETURN_ID, FILING_TYPE, EIN, TAX_PERIOD, SUB_DATE,
TAXPAYER_NAME, RETURN_TYPE, DLN, OBJECT_ID.

For Phase 3a we only need:
  - find_latest_filing(ein, return_types) — locate the most recent
    Schedule-I-bearing filing for a given EIN
  - get_filing_xml(object_id) — pull the XML bytes

Politeness: a small inter-request sleep (the IRS bucket doesn't rate-limit
us but we keep things bounded). In-memory cache of parsed yearly index
files so repeat EIN lookups within a session are free.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import requests

log = logging.getLogger(__name__)


IRS_BASE        = "https://s3.amazonaws.com/irs-form-990"
USER_AGENT      = "GProspect/0.3 (+https://github.com/78k6fcrsh4-rgb/GProspect)"
REQUEST_TIMEOUT = 60.0
MIN_INTERVAL    = 0.5    # IRS S3 doesn't rate-limit; this is just courtesy.

# Return types whose XML contains Schedule I (grants given).
SCHEDULE_I_TYPES = {"990PF", "990"}


@dataclass(frozen=True)
class IndexRow:
    return_id:     str
    filing_type:   str
    ein:           str          # zero-padded 9-digit string
    tax_period:    int          # YYYYMM
    sub_date:      str
    taxpayer_name: str
    return_type:   str          # e.g. "990PF", "990"
    dln:           str
    object_id:     str

    @property
    def tax_year(self) -> int:
        return self.tax_period // 100


class IrsForm990Client:
    """
    Synchronous HTTPS client. Construct once, reuse for an ingestion run.
    Index files are cached in-process so repeat EIN lookups are O(N) over
    the year's filings without re-downloading the CSV.
    """

    def __init__(
        self,
        base_url:        str   = IRS_BASE,
        request_timeout: float = REQUEST_TIMEOUT,
        min_interval:    float = MIN_INTERVAL,
        session:         Optional[requests.Session] = None,
    ):
        self.base_url        = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.min_interval    = min_interval
        self._session        = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_at: float = 0.0
        self._index_cache: dict[int, list[IndexRow]] = {}

    # ── Public methods ───────────────────────────────────────────────────────

    def get_index_year(self, year: int) -> list[IndexRow]:
        """
        Returns the parsed yearly index. Cached after first call.

        Args:
            year: The IRS processing year (NOT the fiscal year on the
                  filing). The IRS files index_YYYY.csv where YYYY is the
                  calendar year the filing was processed.
        """
        if year in self._index_cache:
            return self._index_cache[year]

        url     = f"{self.base_url}/index_{year}.csv"
        text    = self._get_text(url)
        rows    = _parse_index_csv(text)
        self._index_cache[year] = rows
        return rows

    def find_latest_filing(
        self,
        ein:            str,
        return_types:   Iterable[str] = SCHEDULE_I_TYPES,
        years_back:     int           = 3,
        current_year:   Optional[int] = None,
    ) -> Optional[IndexRow]:
        """
        Search recent yearly indexes for the most recent filing matching
        EIN + one of the allowed return_types. Returns None if not found.

        Args:
            ein:          Either "XX-XXXXXXX" or pure-digit.
            return_types: Restrict to these RETURN_TYPE values (default
                          990PF + 990 — both can have Schedule I).
            years_back:   How many calendar years back to look (default 3
                          — covers latest typically processed filings).
            current_year: Override for testability. Defaults to time.gmtime
                          current year.
        """
        norm_ein   = _normalize_ein_digits(ein)
        wanted_rt  = {rt.upper() for rt in return_types}
        if current_year is None:
            current_year = time.gmtime().tm_year

        best: Optional[IndexRow] = None
        for offset in range(0, years_back + 1):
            year = current_year - offset
            try:
                rows = self.get_index_year(year)
            except Exception as e:
                log.warning("IRS index for %s unavailable: %s", year, e)
                continue
            for row in rows:
                if row.ein != norm_ein:
                    continue
                if row.return_type not in wanted_rt:
                    continue
                if best is None or row.tax_period > best.tax_period:
                    best = row
            # If we found a hit in the most recent year, stop searching.
            if best is not None and offset == 0:
                break

        return best

    def find_recent_filings(
        self,
        ein:            str,
        return_types:   Iterable[str] = SCHEDULE_I_TYPES,
        years_back:     int           = 3,
        current_year:   Optional[int] = None,
        max_filings:    int           = 3,
    ) -> list[IndexRow]:
        """
        Return up to `max_filings` recent filings for the EIN, newest
        tax_period first. Used by the ingester to walk back through
        several years for a single funder.
        """
        norm_ein  = _normalize_ein_digits(ein)
        wanted_rt = {rt.upper() for rt in return_types}
        if current_year is None:
            current_year = time.gmtime().tm_year

        found: list[IndexRow] = []
        for offset in range(0, years_back + 1):
            year = current_year - offset
            try:
                rows = self.get_index_year(year)
            except Exception as e:
                log.warning("IRS index for %s unavailable: %s", year, e)
                continue
            for row in rows:
                if row.ein == norm_ein and row.return_type in wanted_rt:
                    found.append(row)
        found.sort(key=lambda r: r.tax_period, reverse=True)
        return found[:max_filings]

    def get_filing_xml(self, object_id: str) -> bytes:
        """Download the raw 990 XML by object_id."""
        url = f"{self.base_url}/{object_id}_public.xml"
        return self._get_bytes(url)

    # ── Internal HTTP ────────────────────────────────────────────────────────

    def _throttle(self) -> None:
        now      = time.monotonic()
        elapsed  = now - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _get_text(self, url: str) -> str:
        self._throttle()
        resp = self._session.get(url, timeout=self.request_timeout)
        if resp.status_code == 404:
            raise FileNotFoundError(url)
        resp.raise_for_status()
        return resp.text

    def _get_bytes(self, url: str) -> bytes:
        self._throttle()
        resp = self._session.get(url, timeout=self.request_timeout)
        if resp.status_code == 404:
            raise FileNotFoundError(url)
        resp.raise_for_status()
        return resp.content


# ─────────────────────────────────────────────────────────────────────────────
# Pure parsers — easy to test
# ─────────────────────────────────────────────────────────────────────────────

def _parse_index_csv(text: str) -> list[IndexRow]:
    """
    Parse an IRS index_YYYY.csv. Tolerant of missing / extra columns:
    only RETURN_ID + EIN + TAX_PERIOD + RETURN_TYPE + OBJECT_ID are
    required. Returns valid rows; logs + skips malformed lines.
    """
    rows: list[IndexRow] = []
    reader = csv.DictReader(io.StringIO(text))
    for line in reader:
        try:
            ein = _normalize_ein_digits(line.get("EIN", ""))
            if not ein:
                continue
            rows.append(IndexRow(
                return_id     = (line.get("RETURN_ID")    or "").strip(),
                filing_type   = (line.get("FILING_TYPE")  or "").strip(),
                ein           = ein,
                tax_period    = int(line.get("TAX_PERIOD") or 0),
                sub_date      = (line.get("SUB_DATE")     or "").strip(),
                taxpayer_name = (line.get("TAXPAYER_NAME") or "").strip(),
                return_type   = (line.get("RETURN_TYPE")  or "").strip().upper(),
                dln           = (line.get("DLN")          or "").strip(),
                object_id     = (line.get("OBJECT_ID")    or "").strip(),
            ))
        except (ValueError, TypeError) as e:
            log.warning("Skipping malformed index row %r: %s", line, e)
            continue
    return rows


def _normalize_ein_digits(ein: str | int) -> str:
    """
    Coerce "31-1234567", 311234567, or "311234567" to "311234567"
    (zero-padded 9-digit string). Returns empty string if unparseable.
    """
    if ein is None:
        return ""
    if isinstance(ein, int):
        return str(ein).rjust(9, "0")
    digits = "".join(c for c in str(ein) if c.isdigit())
    if not digits:
        return ""
    return digits.rjust(9, "0")
