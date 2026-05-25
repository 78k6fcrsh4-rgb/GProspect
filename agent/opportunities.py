"""
agent/opportunities.py
----------------------
Small, pure helpers for opportunity bookkeeping (Phase 1b).

  compute_opportunity_key(funder, program) -> str
    Stable 16-character hex hash for (funder_name, program_name). The
    same (funder, program) tuple always hashes to the same key, so
    pursuit + narrative state persists across agent runs.

  classify_deadline(days_remaining) -> Literal["hot","warm","cold","past"]
    Used to drive the deadline urgency badge on the prospect card and
    the deadline calendar in the weekly digest.

  parse_days_remaining(raw) -> int | None
    The CSVs store days_remaining as a string ("47", "Unknown", ""). This
    helper coerces robustly without raising.

Tested via tests/test_phase1b_opportunities.py.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Key derivation
# ─────────────────────────────────────────────────────────────────────────────

def compute_opportunity_key(funder_name: str, program_name: str) -> str:
    """
    Return a stable 16-character lowercase-hex SHA-256 prefix for the
    (funder_name, program_name) tuple. Whitespace and case are normalized
    so trivial drift ('MacArthur Foundation' vs 'MacArthur  Foundation ')
    doesn't break pursuit-state continuity.

    16 hex chars = 64 bits. Collision probability across a single org's
    pipeline (at most thousands of opportunities) is negligible.

    NOTE: this is NOT a fuzzy match. 'MacArthur Foundation' and
    'John D. and Catherine T. MacArthur Foundation' produce different
    keys. Fuzzy matching is a Phase 2/3 concern.
    """
    f = _normalize_for_key(funder_name)
    p = _normalize_for_key(program_name)
    digest = hashlib.sha256(f"{f}|{p}".encode("utf-8")).hexdigest()
    return digest[:16]


def _normalize_for_key(s: str | None) -> str:
    """Lowercase, strip, collapse internal whitespace."""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", s.strip().lower())


# ─────────────────────────────────────────────────────────────────────────────
# Deadline classification
# ─────────────────────────────────────────────────────────────────────────────

DeadlineBucket = Literal["hot", "warm", "cold", "past", "unknown"]


def classify_deadline(days_remaining: Optional[int]) -> DeadlineBucket:
    """
    Bucket an integer days-to-deadline into the urgency label used on the
    prospect card and digest.

      hot      : 0–30 days  — act this week
      warm     : 31–60 days — start cultivation soon
      cold     : 61+ days   — backlog, monitor
      past     : negative   — deadline already passed
      unknown  : None       — we couldn't parse a deadline

    Boundaries match the PRD ("hot/warm/cold deadline classification"); the
    days-cutoff numbers are deliberately conservative so the "Hot" badge
    earns user attention rather than being noise.
    """
    if days_remaining is None:
        return "unknown"
    if days_remaining < 0:
        return "past"
    if days_remaining <= 30:
        return "hot"
    if days_remaining <= 60:
        return "warm"
    return "cold"


def parse_days_remaining(raw: object) -> Optional[int]:
    """
    Coerce a CSV cell to an int, returning None for anything non-numeric.
    The grant_prospects CSVs store this column as a string and can include
    placeholders like 'Unknown' or empty strings.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or not s.lstrip("-").isdigit():
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None
