"""
tools/source_monitor.py
-----------------------
Phase 5 — source-check engine.

Single entry point: `check_source(db, source) -> SourceCheck`.

Dispatches on MonitoredSource.kind:
  rss    → fetch + parse via xml.etree (no extra dep — feedparser
           would be nicer but is a heavy import for one capability).
  page   → fetch + content-hash + diff against last_content_hash.
  custom → fetch + dispatch to tools.scrapers PARSER_REGISTRY[parser_key].

All paths are defensive: network errors / parse failures are caught
and recorded as FAILED checks, never re-raised. Polite per-host
throttling at >=1 second between requests using a module-level cache.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing  import Optional
from urllib.parse import urlparse

import requests

from portal.models.source           import (
    CheckStatus,
    MonitoredSource,
    SourceCheck,
    SourceKind,
    begin_check,
    finish_check,
)
from tools.scrapers                 import OpportunityItem, get_parser
from tools.scrapers._generic        import normalize_whitespace, strip_tags

log = logging.getLogger(__name__)


REQUEST_TIMEOUT = 30.0
USER_AGENT      = "GProspect/0.4 (+https://github.com/78k6fcrsh4-rgb/GProspect)"
MIN_INTERVAL    = 1.0


# Per-host last-request times for polite throttling.
_last_request_by_host: dict[str, float] = {}


def check_source(db, source: MonitoredSource) -> SourceCheck:
    """
    Run one check against `source`. Persists a SourceCheck audit row
    AND updates source.last_*_at + counts. Caller commits.

    Returns the (post-finish) SourceCheck row.
    """
    check = begin_check(db, source)
    db.flush()

    try:
        if source.kind == SourceKind.RSS:
            items, content_hash = _check_rss(source)
        elif source.kind == SourceKind.CUSTOM:
            items, content_hash = _check_custom(source)
        else:
            items, content_hash = _check_page(source)
    except Exception as e:
        log.exception("Source check failed for %s (id=%s)", source.url, source.id)
        return finish_check(
            db,
            check  = check,
            source = source,
            status = CheckStatus.FAILED,
            message= f"{type(e).__name__}: {e}",
        )

    # Decide success vs unchanged.
    prior_hash = source.last_content_hash
    if content_hash and content_hash == prior_hash:
        status  = CheckStatus.UNCHANGED
        message = (
            f"No change since last check (hash unchanged). "
            f"{len(items)} item(s) recognised on page."
        )
    else:
        status  = CheckStatus.SUCCESS
        message = f"{len(items)} item(s) found."

    return finish_check(
        db,
        check        = check,
        source       = source,
        status       = status,
        items_found  = len(items),
        message      = message,
        content_hash = content_hash,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dispatchers
# ─────────────────────────────────────────────────────────────────────────────

def _check_rss(source: MonitoredSource) -> tuple[list[dict], str]:
    raw = _fetch(source.url)
    items = parse_rss(raw)
    payload_for_hash = "".join(
        sorted(f"{it.get('title','')}|{it.get('link','')}" for it in items)
    )
    return items, _hash(payload_for_hash)


def _check_page(source: MonitoredSource) -> tuple[list[dict], str]:
    raw      = _fetch(source.url)
    text     = strip_tags(raw)
    # Hash the stripped text rather than raw HTML — stable against
    # cookie-script timestamps, CSRF tokens, etc.
    content_hash = _hash(text)
    # No structured items — the "page" kind reports change-detection only.
    return [], content_hash


def _check_custom(source: MonitoredSource) -> tuple[list[dict], str]:
    parser = get_parser(source.parser_key or "")
    if parser is None:
        raise RuntimeError(
            f"No custom parser registered for parser_key={source.parser_key!r}"
        )
    raw  = _fetch(source.url)
    items: list[OpportunityItem] = parser(raw) or []
    payload_for_hash = "".join(
        sorted(f"{it.title}|{it.url or ''}" for it in items)
    )
    return [i.to_dict() for i in items], _hash(payload_for_hash)


# ─────────────────────────────────────────────────────────────────────────────
# Network + parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch(url: str) -> str:
    """Polite GET. Returns response text. Raises on non-2xx."""
    host = (urlparse(url).hostname or "").lower()
    now  = time.monotonic()
    last = _last_request_by_host.get(host, 0.0)
    wait = MIN_INTERVAL - (now - last)
    if wait > 0:
        time.sleep(wait)
    _last_request_by_host[host] = time.monotonic()

    resp = requests.get(
        url,
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"},
        timeout = REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text or ""


def _hash(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Minimal RSS / Atom parser (stdlib-only)
# ─────────────────────────────────────────────────────────────────────────────

def parse_rss(xml_text: str) -> list[dict]:
    """
    Return a list of {title, link, summary, published} dicts.

    Handles RSS 2.0 (<item>) and Atom (<entry>) by walking with
    ElementTree's local-name matching. Defensive on malformed input.
    """
    if not xml_text:
        return []
    try:
        from xml.etree import ElementTree as ET
        root = ET.fromstring(xml_text)
    except Exception as e:
        log.warning("RSS parse failed: %s", e)
        return []

    out: list[dict] = []
    for elem in root.iter():
        ln = elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag
        if ln in ("item", "entry"):
            out.append(_extract_feed_item(elem))
    return [it for it in out if it.get("title")]


def _extract_feed_item(elem) -> dict:
    title:    Optional[str] = None
    link:     Optional[str] = None
    summary:  Optional[str] = None
    published:Optional[str] = None

    for child in elem.iter():
        ln = child.tag.rsplit("}", 1)[-1] if "}" in child.tag else child.tag
        text = (child.text or "").strip()

        if ln == "title" and text and not title:
            title = text
        elif ln == "link":
            # RSS: link is text content; Atom: link is <link href="..." />
            if text and not link:
                link = text
            elif child.attrib.get("href") and not link:
                link = child.attrib["href"]
        elif ln in ("description", "summary", "content") and text and not summary:
            summary = strip_tags(text)
        elif ln in ("pubDate", "published", "updated") and text and not published:
            published = text

    return {
        "title":     normalize_whitespace(title or ""),
        "link":      link,
        "summary":   normalize_whitespace(summary or "")[:600],
        "published": published,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Item summary — used by the API + UI to surface what changed
# ─────────────────────────────────────────────────────────────────────────────

def describe_items(items: list[dict] | None, max_items: int = 5) -> str:
    """Render up to N items as a single human-readable line per item."""
    if not items:
        return ""
    parts: list[str] = []
    for it in items[:max_items]:
        title = it.get("title") or "(no title)"
        link  = it.get("link") or it.get("url") or ""
        if link:
            parts.append(f"- {title} — {link}")
        else:
            parts.append(f"- {title}")
    return "\n".join(parts)
