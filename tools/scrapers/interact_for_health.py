"""
tools/scrapers/interact_for_health.py
-------------------------------------
Custom parser for the Interact for Health grants page.

  Target URL: https://interactforhealth.org/grants/

Smaller Cincinnati-area foundation, simpler site. Their grants page
typically lists program areas with anchor links + summary text. Same
shape as the GCF parser, just different host/selectors.

This module exists so the parser_registry has a third entry for the
Phase 5 acceptance criterion ("Both — generic baseline + custom parsers
for 3 high-priority sources"). EDIT THIS file freely once you've
opened the live page and noted the actual element structure.
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


BASE_URL = "https://interactforhealth.org"

CANDIDATE_SELECTORS = [
    "article",
    "div.grant",
    "div.program",
    "section.grants",
    "li.grant-listing",
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
            link    = elem.find("a", href=True)
            heading = elem.find(["h1", "h2", "h3", "h4"]) or link
            if heading is None:
                continue
            title = normalize_whitespace(heading.get_text())
            if not title or len(title) < 8 or title in seen:
                continue
            body = normalize_whitespace(elem.get_text())[:500]
            if not (looks_like_opportunity(title) or looks_like_opportunity(body)):
                continue
            seen.add(title)
            out.append(OpportunityItem(
                title    = title,
                url      = absolute_url(link.get("href") if link else None, BASE_URL),
                deadline = first_date(body),
                summary  = body,
                metadata = {"source_parser": "interact_for_health", "selector": sel},
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
            metadata = {"source_parser": "interact_for_health", "fallback": True},
        ))
        if len(out) >= 10:
            break
    return out
