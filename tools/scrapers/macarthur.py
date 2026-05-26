"""
tools/scrapers/macarthur.py
---------------------------
Custom parser for the MacArthur Foundation grants page.

  Target URL: https://www.macfound.org/grants/

Assumptions (verified casually — refine after a live test against the
current site):
  - The grants landing page lists program areas, each as a card or
    block with an anchor.
  - Inner program pages are where individual RFPs / open opportunities
    live. The landing page is most useful as a change-detection signal:
    when MacArthur opens/closes a major initiative, the landing page
    text shifts.

The parser returns one item per program-area block found, with
deadline-shaped strings (if any) extracted from the block text. When
selectors fail it falls back to keyword-scan on stripped page text.
"""

from __future__ import annotations

from tools.scrapers          import OpportunityItem
from tools.scrapers._generic import (
    absolute_url,
    first_date,
    looks_like_opportunity,
    normalize_whitespace,
    strip_tags,
)


BASE_URL = "https://www.macfound.org"

# EDIT THIS — selectors are MacArthur's current convention as of the
# 2024 redesign. Adjust if their site team ships a refresh.
CANDIDATE_SELECTORS = [
    "article",
    "div.card",
    "div.program",
    "li.grant",
    "div.entry",
]


def parse(html_text: str) -> list[OpportunityItem]:
    items = _structured_pass(html_text)
    if items:
        return items
    return _fallback_text_scan(html_text)


def _structured_pass(html_text: str) -> list[OpportunityItem]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")

    out: list[OpportunityItem] = []
    seen: set[str] = set()
    for sel in CANDIDATE_SELECTORS:
        for elem in soup.select(sel):
            link = elem.find("a", href=True)
            heading = elem.find(["h1", "h2", "h3", "h4"]) or link
            if heading is None:
                continue
            title = normalize_whitespace(heading.get_text())
            if not title or len(title) < 8 or title in seen:
                continue
            body  = normalize_whitespace(elem.get_text())[:500]
            if not (looks_like_opportunity(title) or looks_like_opportunity(body)):
                continue
            seen.add(title)
            href = link.get("href") if link is not None else None
            out.append(OpportunityItem(
                title    = title,
                url      = absolute_url(href, BASE_URL),
                deadline = first_date(body),
                summary  = body,
                metadata = {"source_parser": "macarthur", "selector": sel},
            ))
    return out


def _fallback_text_scan(html_text: str) -> list[OpportunityItem]:
    text = strip_tags(html_text)
    if not text:
        return []
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
            metadata = {"source_parser": "macarthur", "fallback": True},
        ))
        if len(out) >= 10:
            break
    return out
