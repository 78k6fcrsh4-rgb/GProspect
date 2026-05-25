"""
tools/propublica.py
-------------------
Thin client for the ProPublica Nonprofit Explorer API v2.

Docs: https://projects.propublica.org/nonprofits/api

The wire shape is documented and stable. This wrapper:
  - Returns typed dataclasses (Organization, Filing) instead of raw dicts
    so callers don't depend on the JSON layout.
  - Throttles at <1 req/sec (polite default; rate limits aren't formally
    documented but PDF downloads ARE rate-limited, so we keep it courteous).
  - Defensively parses fields — partial / unexpected responses produce
    Organization rows with None where data is missing, not exceptions.
  - Supports the two search filters we actually use: state (two-letter)
    and ntee major-group (1-10). Specific NTEE codes like "T22" or "P30"
    aren't supported by the search filter; callers post-filter via the
    `ntee_code` field on each Organization.

This module makes NO assumptions about the rest of the codebase — no
DB imports, no FastAPI imports. The discovery cycle (agent/discovery.py)
is the only consumer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)


API_BASE        = "https://projects.propublica.org/nonprofits/api/v2"
REQUEST_TIMEOUT = 30.0
MIN_INTERVAL    = 1.05   # seconds between requests — keep under 1 req/sec
USER_AGENT      = "GProspect/0.2 (+https://github.com/78k6fcrsh4-rgb/GProspect)"


# ─────────────────────────────────────────────────────────────────────────────
# NTEE major group lookup (for the search-filter parameter)
# ─────────────────────────────────────────────────────────────────────────────

NTEE_MAJOR_GROUPS = {
    1:  "Arts, Culture & Humanities",
    2:  "Education",
    3:  "Environment and Animals",
    4:  "Health",
    5:  "Human Services",
    6:  "International, Foreign Affairs",
    7:  "Public, Societal Benefit",      # foundations (T-codes) live here
    8:  "Religion Related",
    9:  "Mutual/Membership Benefit",
    10: "Unknown, Unclassified",
}

# Map a specific NTEE letter prefix (e.g. "T", "P", "L") to the search-API's
# major group number. Derived from the IRS NTEE classification.
NTEE_LETTER_TO_MAJOR = {
    "A": 1,
    "B": 2,
    "C": 3, "D": 3,
    "E": 4, "F": 4, "G": 4, "H": 4,
    "I": 5, "J": 5, "K": 5, "L": 5, "M": 5, "N": 5, "O": 5, "P": 5,
    "Q": 6,
    "R": 7, "S": 7, "T": 7, "U": 7, "V": 7, "W": 7,
    "X": 8,
    "Y": 9,
    "Z": 10,
}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Organization:
    """Normalized organization view from ProPublica's API."""
    ein:        Optional[str]            # "XX-XXXXXXX" string form
    raw_ein:    Optional[int]            # integer EIN as returned
    name:       Optional[str]
    sub_name:   Optional[str]
    address:    Optional[str]
    city:       Optional[str]
    state:      Optional[str]
    zipcode:    Optional[str]
    subseccd:   Optional[int]            # 501(c)(N) subsection code
    ntee_code:  Optional[str]            # specific code, e.g. "T22"
    guidestar_url: Optional[str]
    nccs_url:     Optional[str]

    @property
    def ntee_letter(self) -> Optional[str]:
        if not self.ntee_code:
            return None
        return self.ntee_code[0].upper()

    @property
    def is_foundation(self) -> bool:
        """T-codes are foundations / grantmakers."""
        return self.ntee_letter == "T"


@dataclass
class Filing:
    """Normalized filing view. Only the fields we actually use across
    Phase 2 — the API returns 40-120 additional rows we don't need yet."""
    ein:            Optional[str]
    tax_prd:        Optional[int]        # YYYYMM
    tax_prd_yr:     Optional[int]
    formtype:       Optional[int]        # 0=990, 1=990EZ, 2=990PF
    totrevenue:     Optional[int]
    totfuncexpns:   Optional[int]
    totassetsend:   Optional[int]
    totliabend:     Optional[int]
    pdf_url:        Optional[str]
    updated:        Optional[str]
    extra:          dict[str, Any] = field(default_factory=dict)


@dataclass
class OrganizationDetail:
    """Combined org + filings — what /organizations/:ein.json returns."""
    organization:         Organization
    filings_with_data:    list[Filing]
    filings_without_data: list[Filing]


@dataclass
class SearchPage:
    """One page of search results plus metadata for pagination."""
    organizations:  list[Organization]
    total_results:  int
    num_pages:      int
    cur_page:       int
    per_page:       int


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────

class ProPublicaClient:
    """
    Thin synchronous client. Construct once per discovery run and reuse;
    the rate-limit state is per-instance.

    All public methods raise `requests.HTTPError` on non-2xx responses
    after logging. Callers in the discovery cycle catch and continue —
    one bad org shouldn't kill the whole run.
    """

    def __init__(
        self,
        base_url:       str   = API_BASE,
        request_timeout:float = REQUEST_TIMEOUT,
        min_interval:   float = MIN_INTERVAL,
        session:        Optional[requests.Session] = None,
    ):
        self.base_url        = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.min_interval    = min_interval
        self._session        = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_at: float = 0.0

    # ── Public methods ───────────────────────────────────────────────────────

    def search(
        self,
        q:           Optional[str] = None,
        state:       Optional[str] = None,
        ntee_major:  Optional[int] = None,
        c_code:      Optional[int] = None,
        page:        int           = 0,
    ) -> SearchPage:
        """
        GET /search.json — paginated search for organizations.

        Args:
            q:          Free-text keyword. Use sparingly — quality is mixed.
            state:      Two-letter state code, e.g. "OH". Filters by org state.
            ntee_major: 1-10. NTEE major-group filter.
            c_code:     501(c) subsection code, e.g. 3 for 501(c)(3).
            page:       Zero-indexed page number.

        Returns: SearchPage.
        """
        params: dict[str, Any] = {}
        if q:           params["q"]             = q
        if state:       params["state[id]"]     = state.upper()
        if ntee_major:  params["ntee[id]"]      = int(ntee_major)
        if c_code:      params["c_code[id]"]    = int(c_code)
        if page:        params["page"]          = int(page)

        body = self._get("/search.json", params=params)
        return SearchPage(
            organizations = [_parse_organization(o) for o in body.get("organizations") or []],
            total_results = int(body.get("total_results") or 0),
            num_pages     = int(body.get("num_pages")     or 0),
            cur_page      = int(body.get("cur_page")      or 0),
            per_page      = int(body.get("per_page")      or 25),
        )

    def get_organization(self, ein: str | int) -> OrganizationDetail:
        """
        GET /organizations/:ein.json — full org + filings.

        Args:
            ein: Either the integer EIN or the formatted "XX-XXXXXXX" string.

        Returns: OrganizationDetail.
        """
        norm = _normalize_ein_for_url(ein)
        body = self._get(f"/organizations/{norm}.json")
        org_obj = body.get("organization") or {}
        return OrganizationDetail(
            organization         = _parse_organization(org_obj),
            filings_with_data    = [_parse_filing(f) for f in body.get("filings_with_data")    or []],
            filings_without_data = [_parse_filing(f) for f in body.get("filings_without_data") or []],
        )

    def iter_search(
        self,
        max_results: int = 100,
        **filters,
    ):
        """
        Generator that paginates search results, yielding Organization objects
        until either max_results is reached or there are no more pages.

        Caller is responsible for any post-filtering on specific NTEE codes.
        """
        page = 0
        yielded = 0
        while yielded < max_results:
            sp = self.search(page=page, **filters)
            for org in sp.organizations:
                if yielded >= max_results:
                    return
                yield org
                yielded += 1
            page += 1
            if page >= sp.num_pages:
                return

    # ── Internal HTTP ────────────────────────────────────────────────────────

    def _throttle(self) -> None:
        """Sleep just long enough to keep request rate under self.min_interval."""
        now      = time.monotonic()
        elapsed  = now - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        self._throttle()
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, params=params or {}, timeout=self.request_timeout)
        except requests.RequestException as e:
            log.warning("ProPublica request failed: %s %s", url, e)
            raise
        if resp.status_code >= 400:
            log.warning(
                "ProPublica %s returned HTTP %s for path=%s params=%s",
                resp.request.method, resp.status_code, path, params,
            )
        resp.raise_for_status()
        return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Parsers — pure functions, easy to test
# ─────────────────────────────────────────────────────────────────────────────

def _parse_organization(d: dict) -> Organization:
    if not isinstance(d, dict):
        d = {}
    raw_ein = _try_int(d.get("ein"))
    return Organization(
        ein           = d.get("strein") or _format_ein(raw_ein),
        raw_ein       = raw_ein,
        name          = (d.get("name") or "").strip() or None,
        sub_name      = (d.get("sub_name") or "").strip() or None,
        address       = (d.get("address") or "").strip() or None,
        city          = (d.get("city")    or "").strip() or None,
        state         = (d.get("state")   or "").strip() or None,
        zipcode       = (d.get("zipcode") or "").strip() or None,
        subseccd      = _try_int(d.get("subseccd")),
        ntee_code     = (d.get("ntee_code") or "").strip() or None,
        guidestar_url = d.get("guidestar_url") or None,
        nccs_url      = d.get("nccs_url")      or None,
    )


_FILING_KNOWN = {
    "ein", "tax_prd", "tax_prd_yr", "formtype",
    "totrevenue", "totfuncexpns", "totassetsend", "totliabend",
    "pdf_url", "updated", "organization",
}


def _parse_filing(d: dict) -> Filing:
    if not isinstance(d, dict):
        d = {}
    raw_ein = _try_int(d.get("ein"))
    extra = {k: v for k, v in d.items() if k not in _FILING_KNOWN}
    return Filing(
        ein          = _format_ein(raw_ein) or (str(raw_ein) if raw_ein else None),
        tax_prd      = _try_int(d.get("tax_prd")),
        tax_prd_yr   = _try_int(d.get("tax_prd_yr")),
        formtype     = _try_int(d.get("formtype")),
        totrevenue   = _try_int(d.get("totrevenue")),
        totfuncexpns = _try_int(d.get("totfuncexpns")),
        totassetsend = _try_int(d.get("totassetsend")),
        totliabend   = _try_int(d.get("totliabend")),
        pdf_url      = d.get("pdf_url") or None,
        updated      = d.get("updated") or None,
        extra        = extra,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _try_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _format_ein(raw: Optional[int]) -> Optional[str]:
    """Convert integer EIN to XX-XXXXXXX with leading-zero padding."""
    if raw is None:
        return None
    s = str(int(raw)).rjust(9, "0")
    return f"{s[:2]}-{s[2:]}"


def _normalize_ein_for_url(ein: str | int) -> str:
    """
    The /organizations/:ein endpoint takes the integer form. Accept either
    the integer or the "XX-XXXXXXX" string and produce the integer string.
    """
    if isinstance(ein, int):
        return str(ein)
    digits = "".join(c for c in str(ein) if c.isdigit())
    if not digits:
        raise ValueError(f"Could not parse EIN: {ein!r}")
    return str(int(digits))
