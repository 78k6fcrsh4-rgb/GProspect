"""
tools/scrapers/gcf.py
---------------------
Custom parser for Greater Cincinnati Foundation grant pages.

  Target URL: https://www.gcfdn.org/grants/

Assumptions (verified casually, not exhaustively — refine after a live
test against the current site):
  - Grant opportunities live inside <article> or <div class*="grant">
    elements on the page.
  - Each opportunity has an anchor <a> with the program name.
  - Deadlines are typically rendered as plain text near the title,
    matching common date formats handled by _generic.first_date.

If the structured selectors return nothing, we fall back to scanning
the whole page text for "grant"/"RFP" keyword lines + first date match
on the same line. That keeps the parser useful even if GCF restructures
their site.
"""

from __future__ import annotations

from typing import Optional

from tools.scrapers          import OpportunityItem
from tools.scrapers._generic import (
    absolute_url,
    first_date,
    looks_like_opportunity,
    normalize_whitespace,
    strip_tags,
)


BASE_URL = "https://www.gcfdn.org"

# EDIT THIS if the page structure changes — these are the CSS-selectors
# the structured pass tries first.
CANDIDATE_SELECTORS = [
    "article",
    "div.grant",
    "div.grants",
    "div.program",
    "li.grant",
]


def parse(html_text: str) -> list[OpportunityItem]:
    items = _structured_pass(html_text)
    if items:
        return items
    return _fallback_text_scan(html_text)


def _structured_pass(html_text: str) -> list[OpportunityItem]:
    """Try BeautifulSoup-driven selectors. Return [] on any failure."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")

    found: list[OpportunityItem] = []
    seen_titles: set[str] = set()

    for sel in CANDIDATE_SELECTORS:
        for elem in soup.select(sel):
            title_node = elem.find("a") or elem.find(["h2", "h3", "h4"])
            if title_node is None:
                continue
            title = normalize_whitespace(title_node.get_text())
            if not title or len(title) < 8 or title in seen_titles:
                continue
            if not looks_like_opportunity(title) and not looks_like_opportunity(elem.get_text()):
                continue
            seen_titles.add(title)
            href = title_node.get("href") if hasattr(title_node, "get") else None
            url  = absolute_url(href, BASE_URL)
            body = normalize_whitespace(elem.get_text())[:500]
            found.append(OpportunityItem(
                title    = title,
                url      = url,
                deadline = first_date(body),
                summary  = body,
                metadata = {"source_parser": "gcf", "selector": sel},
            ))
    return found


def _fallback_text_scan(html_text: str) -> list[OpportunityItem]:
    """When structure fails, scan the page text for keyword lines."""
    text = strip_tags(html_text)
    if not text:
        return []
    # Split on sentence-ish boundaries; keep up to ~10 lines that look
    # opportunity-shaped.
    out: list[OpportunityItem] = []
    for line in text.split(". "):
        line = normalize_whitespace(line)
        if not line or len(line) < 20 or len(line) > 400:
            continue
        if not looks_like_opportunity(line):
            continue
        out.append(OpportunityItem(
            title    = line[:140],
            url      = BASE_URL + "/grants/",
            deadline = first_date(line),
            summary  = line,
            metadata = {"source_parser": "gcf", "fallback": True},
        ))
        if len(out) >= 10:
            break
    return out
