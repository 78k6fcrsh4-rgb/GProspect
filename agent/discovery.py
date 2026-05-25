"""
agent/discovery.py
------------------
Phase 2 — funder discovery cycle.

Given an OrgProfile, search ProPublica for foundations that plausibly
fit the org's geography, size, and grantmaking focus. Score each
candidate. Upsert FunderCandidate rows so re-running the cycle
refreshes scores without losing the user's status decisions.

Design choices documented in the v2 PRD ("Phase 2 — Tier 3 Discovery"):

  - Use ProPublica's search API for the candidate pool. NTEE major
    group 7 ("Public, Societal Benefit") includes all T-codes
    (foundations). State filter narrows to the org's state.
  - Post-filter to T-codes for "is a foundation."
  - For top candidates, fetch detailed financials to enrich the
    score with asset size + filing recency signals.
  - Cap network usage: ≤75 search results pulled (3 pages),
    ≤30 detailed lookups. At 1 req/sec ≈ 33s per discovery run.
  - This is the "plausible-fit" v2 of discovery. Phase 3 upgrades to
    true peer-grant analysis once we ingest IRS bulk 990 data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from agent.profile                   import OrgProfile
from portal.models.funder_candidate  import upsert_candidate
from tools.propublica                import (
    NTEE_LETTER_TO_MAJOR,
    Organization,
    OrganizationDetail,
    ProPublicaClient,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

# How many search results to pull before scoring + filtering.
MAX_SEARCH_RESULTS = 75

# How many detail lookups to do per discovery run (one ProPublica call each).
MAX_DETAIL_LOOKUPS = 30

# Score weights (out of 5.0 total).
WEIGHT_STATE_MATCH      = 1.5
WEIGHT_COMMUNITY_FDN    = 1.0  # T31 — most likely to fund local
WEIGHT_PRIVATE_GRANTMKR = 0.5  # T20 / T22
WEIGHT_SIZE_MATCH       = 1.0
WEIGHT_RECENT_FILING    = 0.5
WEIGHT_NTEE_BREADTH     = 0.5  # T-class catch-all extra credit


# ─────────────────────────────────────────────────────────────────────────────
# Result type — what discover_funders returns
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DiscoveryResult:
    candidates_inserted: int
    candidates_refreshed: int
    candidates_seen:     int   # total foundations evaluated
    search_calls:        int
    detail_calls:        int
    notes:               list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def discover_funders(
    *,
    db,
    org_id:           int,
    profile:          OrgProfile,
    client:           Optional[ProPublicaClient] = None,
    max_search:       int = MAX_SEARCH_RESULTS,
    max_detail:       int = MAX_DETAIL_LOOKUPS,
) -> DiscoveryResult:
    """
    Run one discovery cycle for an organization.

    Args:
        db:      SQLAlchemy session — caller commits.
        org_id:  The Organization row id to attach candidates to.
        profile: The org's current OrgProfile (Pydantic, validated).
        client:  Optional ProPublicaClient. Tests inject a stub.
        max_search / max_detail: caps for the run.

    Returns: DiscoveryResult with counts and any notes worth surfacing.
    """
    client    = client or ProPublicaClient()
    state     = profile.geography.state
    notes:    list[str] = []
    inserted  = 0
    refreshed = 0
    seen      = 0
    n_search  = 0
    n_detail  = 0

    # ── 1. Search ───────────────────────────────────────────────────────────
    # Two passes:
    #   a) (state, ntee_major=7) — all foundations + public-benefit orgs in
    #      the org's home state. T-codes (foundations) are the target.
    #   b) (state, ntee_major matching the org's primary work) — picks up
    #      foundations classified under their funding focus (some are
    #      classified P / L / B etc rather than T).
    candidate_orgs: dict[str, Organization] = {}

    pass_a_count = 0
    for org in client.iter_search(
        max_results = max_search,
        state       = state,
        ntee_major  = 7,
        c_code      = 3,
    ):
        n_search = max(n_search, 1)  # at least one page was fetched
        if not _looks_like_foundation(org):
            continue
        if org.ein:
            candidate_orgs[org.ein] = org
            pass_a_count += 1

    notes.append(
        f"Pass A (state={state}, NTEE major 7): "
        f"{pass_a_count} foundation candidate(s) after T-code filter."
    )

    # Pass b — broaden to ntee majors implied by program areas.
    program_majors = _program_areas_to_ntee_majors(profile)
    if program_majors:
        for major in sorted(program_majors):
            if major == 7:
                continue   # already covered in pass A
            for org in client.iter_search(
                max_results = max_search // 2,
                state       = state,
                ntee_major  = major,
                c_code      = 3,
            ):
                if not _looks_like_foundation(org):
                    continue
                if org.ein and org.ein not in candidate_orgs:
                    candidate_orgs[org.ein] = org
        notes.append(
            f"Pass B (broader NTEE majors {sorted(program_majors)}): "
            f"{len(candidate_orgs) - pass_a_count} additional candidates."
        )

    seen = len(candidate_orgs)

    # ── 2. Initial score from search-stage data ────────────────────────────
    # Score what we have. We'll only fetch detail for the top-N to keep
    # ProPublica traffic bounded.
    scored: list[tuple[float, Organization, dict]] = []
    for org in candidate_orgs.values():
        score, signals = _score_from_org(org, profile)
        scored.append((score, org, signals))
    scored.sort(key=lambda triple: triple[0], reverse=True)

    # ── 3. Enrich the top candidates with /organizations/:ein details ─────
    top_n = scored[:max_detail]
    enriched_scores: dict[str, tuple[float, dict, OrganizationDetail | None]] = {}
    for s, org, signals in top_n:
        if not org.ein:
            continue
        detail: OrganizationDetail | None = None
        try:
            detail   = client.get_organization(org.ein)
            n_detail += 1
        except Exception as e:
            log.warning("ProPublica detail fetch failed for %s: %s", org.ein, e)
        s2, sig2 = _enrich_score(s, signals, org, detail, profile)
        enriched_scores[org.ein] = (s2, sig2, detail)

    # Remaining (lower-scored) candidates: keep their initial scores.
    for s, org, signals in scored[max_detail:]:
        if org.ein and org.ein not in enriched_scores:
            enriched_scores[org.ein] = (s, signals, None)

    # ── 4. Upsert into the DB ──────────────────────────────────────────────
    for ein, (score, signals, _detail) in enriched_scores.items():
        org_obj  = candidate_orgs[ein]
        rationale = _build_rationale(org_obj, score, signals, profile)
        _, was_inserted = upsert_candidate(
            db,
            org_id         = org_id,
            ein            = ein,
            funder_name    = org_obj.name or "(unnamed)",
            funder_city    = org_obj.city,
            funder_state   = org_obj.state,
            funder_zipcode = org_obj.zipcode,
            ntee_code      = org_obj.ntee_code,
            subseccd       = org_obj.subseccd,
            score          = round(score, 3),
            rationale      = rationale,
            signals        = signals,
        )
        if was_inserted:
            inserted += 1
        else:
            refreshed += 1

    notes.append(
        f"Upserted {inserted + refreshed} candidates ({inserted} new, {refreshed} refreshed)."
    )
    return DiscoveryResult(
        candidates_inserted  = inserted,
        candidates_refreshed = refreshed,
        candidates_seen      = seen,
        search_calls         = n_search,
        detail_calls         = n_detail,
        notes                = notes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Filtering + scoring
# ─────────────────────────────────────────────────────────────────────────────

def _looks_like_foundation(org: Organization) -> bool:
    """T-codes are foundations / grantmakers."""
    return bool(org.ntee_letter and org.ntee_letter == "T")


def _program_areas_to_ntee_majors(profile: OrgProfile) -> set[int]:
    """
    Translate the org's program areas (string enum) into NTEE major
    groups so we can broaden the search beyond just T-coded foundations.

    Mapping is intentionally permissive — when in doubt, include.
    """
    mapping = {
        "housing_permanent":        5,   # Human Services
        "housing_transitional":     5,
        "housing_rapid_rehousing":  5,
        "domestic_violence":        5,
        "food_security":            5,
        "workforce_development":    5,
        "reentry":                  5,
        "legal_services":           5,
        "childcare":                5,
        "education":                2,   # Education
        "financial_literacy":       2,
        "healthcare":               4,   # Health
        "mental_health":            4,
        "substance_use":            4,
        "general_operating":        7,   # Public, Societal Benefit
    }
    out: set[int] = set()
    for area in profile.program_areas or []:
        key = area.value if hasattr(area, "value") else area
        m = mapping.get(key)
        if m:
            out.add(m)
    return out


def _score_from_org(org: Organization, profile: OrgProfile) -> tuple[float, dict]:
    """Score a candidate using only what's in the search-result Organization."""
    signals: dict = {
        "state_match":           False,
        "is_community_fdn":      False,
        "is_private_grantmaker": False,
        "ntee_code":             org.ntee_code,
        "asset_size_match":      None,    # set during enrichment
        "recent_filing":         None,    # set during enrichment
    }
    score = 0.0

    # State match
    if org.state and profile.geography.state and org.state.upper() == profile.geography.state.upper():
        score += WEIGHT_STATE_MATCH
        signals["state_match"] = True

    # Community foundation
    if (org.ntee_code or "").upper().startswith("T31"):
        score += WEIGHT_COMMUNITY_FDN
        signals["is_community_fdn"] = True
    # Private grantmaking / independent / corporate (T20 / T22 / T21)
    if (org.ntee_code or "").upper().startswith(("T20", "T22", "T21")):
        score += WEIGHT_PRIVATE_GRANTMKR
        signals["is_private_grantmaker"] = True

    # Generic credit for any T-class (catch-all foundation)
    if (org.ntee_code or "").upper().startswith("T"):
        score += WEIGHT_NTEE_BREADTH

    return score, signals


def _enrich_score(
    base_score: float,
    base_signals: dict,
    org:     Organization,
    detail:  OrganizationDetail | None,
    profile: OrgProfile,
) -> tuple[float, dict]:
    """Add signals available only after fetching detailed financials."""
    score   = base_score
    signals = dict(base_signals)

    if detail is None:
        signals["asset_size_match"] = "unknown"
        signals["recent_filing"]    = "unknown"
        return score, signals

    # Most recent filing with financial data
    latest = _latest_filing(detail.filings_with_data)
    if latest is None:
        signals["asset_size_match"] = "no_filings"
        signals["recent_filing"]    = "none"
        return score, signals

    # Asset size — rough heuristic: foundations whose ending assets are at
    # least 50x the org's max request can plausibly write that-size grants.
    request_ceiling = profile.budget.request_ceiling
    assets = latest.totassetsend
    if assets is not None and request_ceiling:
        if assets >= 50 * request_ceiling:
            score += WEIGHT_SIZE_MATCH
            signals["asset_size_match"] = "good"
        elif assets >= 10 * request_ceiling:
            score += WEIGHT_SIZE_MATCH * 0.5
            signals["asset_size_match"] = "marginal"
        else:
            signals["asset_size_match"] = "too_small"
    else:
        signals["asset_size_match"] = "unknown"

    # Recency — filed within the last 2 years
    if latest.tax_prd_yr is not None:
        import datetime as _dt
        if latest.tax_prd_yr >= _dt.datetime.now(_dt.timezone.utc).year - 2:
            score += WEIGHT_RECENT_FILING
            signals["recent_filing"] = "recent"
        else:
            signals["recent_filing"] = "stale"
            signals["latest_tax_year"] = latest.tax_prd_yr

    signals["assets_end_year"] = assets
    return score, signals


def _latest_filing(filings) -> object | None:
    """Return the filing with the largest tax_prd, or None."""
    if not filings:
        return None
    rated = [(f.tax_prd or 0, f) for f in filings]
    rated.sort(key=lambda t: t[0], reverse=True)
    return rated[0][1] if rated else None


# ─────────────────────────────────────────────────────────────────────────────
# Rationale rendering
# ─────────────────────────────────────────────────────────────────────────────

def _build_rationale(
    org:     Organization,
    score:   float,
    signals: dict,
    profile: OrgProfile,
) -> str:
    """Render a human-readable rationale string from the matched signals."""
    bits: list[str] = []
    if signals.get("state_match"):
        bits.append(f"Based in {org.state}, matching your geography ({profile.geography.city}, {profile.geography.state}).")
    elif org.state:
        bits.append(f"Based in {org.state}.")

    code = (org.ntee_code or "").upper()
    if signals.get("is_community_fdn"):
        bits.append("Community foundation — typically funds local nonprofits.")
    elif signals.get("is_private_grantmaker"):
        bits.append(f"Private grantmaking foundation (NTEE {code}).")
    elif code.startswith("T"):
        bits.append(f"Foundation (NTEE {code}).")

    if signals.get("asset_size_match") == "good":
        assets = signals.get("assets_end_year")
        if assets:
            bits.append(
                f"Ending assets ${assets:,.0f} — comfortably supports grants in your "
                f"${profile.budget.request_floor:,}-${profile.budget.request_ceiling:,} range."
            )
        else:
            bits.append("Asset size aligned with your request range.")
    elif signals.get("asset_size_match") == "marginal":
        bits.append("Asset size could potentially support your grant range.")
    elif signals.get("asset_size_match") == "too_small":
        bits.append("Asset size suggests they fund smaller grants than your floor; lower priority.")

    if signals.get("recent_filing") == "recent":
        bits.append("Active — filed a Form 990 within the last 2 years.")
    elif signals.get("recent_filing") == "stale":
        bits.append(
            f"Last filing on record is from {signals.get('latest_tax_year')}; "
            f"verify they're still active before investing time."
        )

    if not bits:
        bits.append("Surfaced by NTEE + geography match. Review for fit.")

    return " ".join(bits)
