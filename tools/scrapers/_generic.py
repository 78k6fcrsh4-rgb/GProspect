"""
tools/scrapers/_generic.py
--------------------------
Shared helpers for foundation-specific parsers. Most foundation grant
pages share a common structure (header + list of links + sometimes a
deadline date). Use these as the fallback path inside per-foundation
modules so a parser whose specific selectors miss still returns *some*
signal rather than zero.
"""

from __future__ import annotations

import re
from typing import Optional

OPPORTUNITY_KEYWORDS = {
    "grant", "rfp", "request for proposal", "funding", "apply", "application",
    "letter of inquiry", "loi", "deadline", "open", "now accepting",
}

# Loose deadline-date matcher: "March 15, 2026", "Mar 15", "3/15/2026", "2026-03-15"
DATE_PATTERNS = [
    r"(?P<m1>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?",
    r"\d{1,2}/\d{1,2}/\d{2,4}",
    r"\d{4}-\d{2}-\d{2}",
]
DATE_RE = re.compile("|".join(DATE_PATTERNS), re.IGNORECASE)


def looks_like_opportunity(text: str) -> bool:
    """Cheap keyword check on a line of text or link label."""
    if not text:
        return False
    low = text.lower()
    return any(kw in low for kw in OPPORTUNITY_KEYWORDS)


def first_date(text: str) -> Optional[str]:
    """Return the first deadline-shaped string found in `text`, or None."""
    if not text:
        return None
    m = DATE_RE.search(text)
    return m.group(0) if m else None


def normalize_whitespace(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def absolute_url(href: str | None, base: str) -> Optional[str]:
    """Resolve a possibly-relative href against the base URL."""
    if not href:
        return None
    href = href.strip()
    if href.startswith(("http://", "https://", "mailto:")):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        # Naive — strip the path from base and append. Works for the
        # foundation sites we care about.
        from urllib.parse import urlparse
        parsed = urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    # Relative without leading slash — append.
    if base.endswith("/"):
        return base + href
    return base + "/" + href


# A very-lightweight HTML tag stripper. Used as a fallback when the
# parser can't find structured anchors. Avoids pulling in beautifulsoup
# in the hot path (we do import it inside per-foundation parsers below).
_TAG_RE     = re.compile(r"<[^>]+>")
_SCRIPT_RE  = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def strip_tags(html: str) -> str:
    """Coarse HTML-to-text. Returns whitespace-normalized text."""
    if not html:
        return ""
    no_script = _SCRIPT_RE.sub(" ", html)
    text      = _TAG_RE.sub(" ", no_script)
    return normalize_whitespace(text)
