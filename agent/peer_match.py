"""
agent/peer_match.py
-------------------
Phase 3b — peer-org identification + warm-path inference.

A "peer" is a recipient organization that resembles the user's org on
mission-shape signals available in the ingested 990 data: same state,
name contains keywords implied by the user's program areas / mission
keywords / populations served. We're deliberately permissive — false
positives surface for human review, which is the right error direction
for the prospecting use case.

A "warm path" for a (funder, user_org) pair is the list of grants
the funder has given to peer organizations of the user's org. The
PRD acceptance criterion: "≥3 warm-path suggestions on the dashboard
for the pilot Cincinnati CBO within 48 hours of profile save."

Pure functions here — easy to test. The router in
portal/routers/funders.py wires them to the API surface.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from agent.profile           import OrgProfile
from portal.models.grant     import Funder, Grant, RecipientOrg

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Keyword expansion — turns a profile into a list of name-match terms
# ─────────────────────────────────────────────────────────────────────────────

# Each program-area value maps to a list of name-fragments we'd expect to
# see in peer recipient orgs working in that program area. Intentionally
# broad — we'd rather surface a false positive a human can dismiss than
# miss a real peer.
PROGRAM_AREA_KEYWORDS: dict[str, list[str]] = {
    "housing_permanent":       ["housing", "homes", "homeless", "shelter", "supportive housing"],
    "housing_transitional":    ["housing", "transitional", "shelter", "homeless"],
    "housing_rapid_rehousing": ["housing", "rehousing", "homeless"],
    "domestic_violence":       ["violence", "abuse", "shelter", "women", "safe"],
    "workforce_development":   ["workforce", "employment", "jobs", "training", "career"],
    "food_security":           ["food", "pantry", "hunger", "nutrition", "meal"],
    "healthcare":              ["health", "clinic", "medical", "care"],
    "mental_health":           ["mental health", "counseling", "behavioral", "psych"],
    "substance_use":           ["recovery", "addiction", "substance", "treatment"],
    "reentry":                 ["reentry", "re-entry", "returning citizens"],
    "legal_services":          ["legal", "law", "advocacy", "rights"],
    "childcare":               ["childcare", "child care", "daycare", "early childhood"],
    "education":               ["education", "school", "academy", "tutoring", "literacy", "learning"],
    "financial_literacy":      ["financial", "literacy", "credit", "savings"],
    "general_operating":       [],   # Too broad; rely on populations + mission_keywords
}

POPULATION_KEYWORDS: dict[str, list[str]] = {
    "women":                  ["women", "girls"],
    "men":                    ["men", "boys"],
    "youth":                  ["youth", "children", "kids", "young", "teen", "adolescent"],
    "seniors":                ["senior", "elder", "aging"],
    "families":               ["family", "families", "parenting"],
    "veterans":               ["veteran", "military"],
    "lgbtq":                  ["lgbtq", "lgbt", "queer", "pride"],
    "immigrants":             ["immigrant", "refugee", "newcomer"],
    "formerly_incarcerated":  ["reentry", "returning citizens", "formerly incarcerated"],
    "survivors_dv":           ["survivors", "violence", "abuse"],
    "chronically_homeless":   ["homeless", "housing"],
    "low_income":             [],   # Too broad
    "disabled":               ["disability", "disabled", "accessibility"],
    "bipoc":                  ["black", "latino", "latina", "asian", "indigenous"],
    "mental_health":          ["mental health", "behavioral"],
}


def _enum_value(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


def expand_keywords(profile: OrgProfile) -> list[str]:
    """
    Build a deduplicated list of name-match keywords from the profile.

    Sources, in order of inclusion:
      1. mission_keywords (already free-text the user provided)
      2. Each program_area's keyword cluster
      3. Each populations_served population's keyword cluster

    Keywords are lowercased and trimmed. Caller decides how to match
    (substring vs. word-boundary); see name_matches_keywords() below.
    """
    out: list[str] = []
    seen: set[str] = set()

    for kw in (profile.mission_keywords or []):
        norm = (kw or "").strip().lower()
        if norm and norm not in seen:
            out.append(norm)
            seen.add(norm)

    for area in (profile.program_areas or []):
        for kw in PROGRAM_AREA_KEYWORDS.get(_enum_value(area), []):
            norm = kw.strip().lower()
            if norm and norm not in seen:
                out.append(norm)
                seen.add(norm)

    for pop in (profile.populations_served or []):
        for kw in POPULATION_KEYWORDS.get(_enum_value(pop), []):
            norm = kw.strip().lower()
            if norm and norm not in seen:
                out.append(norm)
                seen.add(norm)

    return out


def name_matches_keywords(name: str | None, keywords: Iterable[str]) -> list[str]:
    """
    Return the subset of keywords that appear in `name` (case-insensitive,
    word-boundary aware for single tokens; substring for multi-word terms
    so phrases like 'mental health' match without breaking).
    """
    if not name:
        return []
    n = name.lower()
    matched: list[str] = []
    for kw in keywords:
        if " " in kw:
            # Multi-word keyword: substring match
            if kw in n:
                matched.append(kw)
        else:
            # Single-word: word-boundary match
            if re.search(rf"\b{re.escape(kw)}\b", n):
                matched.append(kw)
    return matched


# ─────────────────────────────────────────────────────────────────────────────
# Peer scoring
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PeerScore:
    is_peer:  bool
    score:    float      # 0.0 - 3.0 internal; not surfaced directly
    reasons:  list[str]


def score_peer_match(
    recipient: RecipientOrg,
    profile:   OrgProfile,
    keywords:  Optional[list[str]] = None,
) -> PeerScore:
    """
    Score how much a recipient looks like a peer of the user's org.

    Heuristic (additive):
      +1.5  same state as user's org
      +1.0  name contains any expanded-keyword match
      +0.5  city match (only if state already matches)
      +0.5  recipient has an EIN matching one of the user's known_funders'
            past recipients (not implemented yet — left as a future hook)

    Returns is_peer = True when score >= 1.5 (i.e., at minimum a state
    match — name match alone isn't enough since recipient lists are
    huge nationally).
    """
    kws = keywords if keywords is not None else expand_keywords(profile)
    score = 0.0
    reasons: list[str] = []

    user_state = (profile.geography.state or "").upper()
    rec_state  = (recipient.state or "").upper()
    if user_state and rec_state and user_state == rec_state:
        score += 1.5
        reasons.append(f"based in {rec_state}")

    matched_kws = name_matches_keywords(recipient.name, kws)
    if matched_kws:
        score += 1.0
        # Surface at most 3 matched keywords so the rationale stays short
        shown = matched_kws[:3]
        reasons.append("name matches " + ", ".join(f"'{k}'" for k in shown))

    user_city = (profile.geography.city or "").lower()
    rec_city  = (recipient.city or "").lower()
    if score >= 1.5 and user_city and rec_city and user_city == rec_city:
        score += 0.5
        reasons.append(f"based in {recipient.city}")

    return PeerScore(
        is_peer = score >= 1.5,
        score   = score,
        reasons = reasons,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Warm-path query — for one funder, peer grants given
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PeerGrantHit:
    """One grant from a funder to a peer recipient, with match reasons."""
    grant_id:        int
    recipient_id:    int
    recipient_name:  str
    recipient_city:  Optional[str]
    recipient_state: Optional[str]
    recipient_ein:   Optional[str]
    fiscal_year:     Optional[int]
    amount:          Optional[int]
    purpose:         Optional[str]
    score:           float
    reasons:         list[str]


def find_peer_grants_for_funder(
    db:        Session,
    profile:   OrgProfile,
    funder_ein:str,
    limit:     int = 25,
) -> list[PeerGrantHit]:
    """
    Find peer-org grants that the funder identified by `funder_ein`
    has given.

    Algorithm:
      1. Join Grant → Funder → RecipientOrg
      2. Filter to the funder's grants
      3. Pre-filter recipients by state == user_state (fast, indexed)
      4. Score each candidate peer; keep those with is_peer == True
      5. Sort by score desc, then fiscal_year desc, then amount desc
    """
    user_state = (profile.geography.state or "").upper()
    if not user_state:
        return []

    rows = (
        db.query(Grant, RecipientOrg)
          .join(Funder, Grant.funder_id == Funder.id)
          .join(RecipientOrg, Grant.recipient_id == RecipientOrg.id)
          .filter(Funder.ein == funder_ein)
          .filter(RecipientOrg.state == user_state)
          .all()
    )
    if not rows:
        return []

    keywords = expand_keywords(profile)
    hits: list[PeerGrantHit] = []
    for grant, recipient in rows:
        score = score_peer_match(recipient, profile, keywords=keywords)
        if not score.is_peer:
            continue
        hits.append(PeerGrantHit(
            grant_id        = grant.id,
            recipient_id    = recipient.id,
            recipient_name  = recipient.name,
            recipient_city  = recipient.city,
            recipient_state = recipient.state,
            recipient_ein   = recipient.ein,
            fiscal_year     = grant.fiscal_year,
            amount          = grant.amount,
            purpose         = grant.purpose,
            score           = score.score,
            reasons         = score.reasons,
        ))

    hits.sort(
        key     = lambda h: (
            -h.score,
            -(h.fiscal_year or 0),
            -(h.amount       or 0),
        ),
    )
    return hits[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# Warm-path summary — across all candidate funders, count peer grants each
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WarmPathSummary:
    funder_ein:        str
    funder_name:       str
    peer_grant_count:  int
    most_recent_year:  Optional[int]
    total_amount:      Optional[int]


def warm_path_summary_for_org(
    db:         Session,
    profile:    OrgProfile,
    funder_eins:Iterable[str],
) -> list[WarmPathSummary]:
    """
    For each candidate funder EIN, return a one-row summary of peer-grant
    activity. Cheaper than fetching the full list of hits per candidate;
    the Funders dashboard uses this for a row-level "3 warm paths" badge.
    """
    out: list[WarmPathSummary] = []
    for ein in funder_eins:
        hits = find_peer_grants_for_funder(db, profile, ein, limit=1000)
        if not hits:
            # Look up funder name even when there's no warm path so the
            # UI can show a uniform list.
            f = db.query(Funder).filter(Funder.ein == ein).one_or_none()
            out.append(WarmPathSummary(
                funder_ein       = ein,
                funder_name      = f.name if f else "",
                peer_grant_count = 0,
                most_recent_year = None,
                total_amount     = None,
            ))
            continue
        f = db.query(Funder).filter(Funder.ein == ein).one_or_none()
        total = sum((h.amount or 0) for h in hits)
        out.append(WarmPathSummary(
            funder_ein       = ein,
            funder_name      = f.name if f else "",
            peer_grant_count = len(hits),
            most_recent_year = max(h.fiscal_year or 0 for h in hits) or None,
            total_amount     = total or None,
        ))
    return out
