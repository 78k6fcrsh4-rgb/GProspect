"""
tools/scrapers
--------------
Per-foundation custom parsers for Phase 5.

Each module exposes a `parse(html_text) -> list[OpportunityItem]`
function. The PARSER_REGISTRY dict maps parser_key strings (stored on
MonitoredSource.parser_key) to the callable.

Adding a new foundation parser:
  1. Create a new file in this directory exporting `parse(html_text)`.
  2. Register it in PARSER_REGISTRY below.
  3. Add a MonitoredSource row with kind='custom' and parser_key
     matching the registry key.

The parsers are deliberately permissive: every selector / keyword
assumption is documented inline ("# EDIT THIS if the page restructures")
so an admin can refine without reverse-engineering. Each parser also
falls back to the generic-page heuristics when its specific selectors
return nothing — so a parser that goes stale degrades to baseline
rather than silently failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing      import Callable, Optional


@dataclass
class OpportunityItem:
    """One opportunity-like item discovered on a source page."""
    title:    str
    url:      Optional[str] = None
    deadline: Optional[str] = None
    summary:  Optional[str] = None
    metadata: dict          = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "title":    self.title,
            "url":      self.url,
            "deadline": self.deadline,
            "summary":  self.summary,
            "metadata": self.metadata,
        }


# Type for parse functions.
ParseFn = Callable[[str], list[OpportunityItem]]


def _build_registry() -> dict[str, ParseFn]:
    """Lazy import the parser modules so a single broken parser doesn't
    poison registry initialization."""
    registry: dict[str, ParseFn] = {}
    try:
        from tools.scrapers import gcf
        registry["gcf"] = gcf.parse
    except Exception:
        pass
    try:
        from tools.scrapers import macarthur
        registry["macarthur"] = macarthur.parse
    except Exception:
        pass
    try:
        from tools.scrapers import interact_for_health
        registry["interact_for_health"] = interact_for_health.parse
    except Exception:
        pass
    return registry


PARSER_REGISTRY: dict[str, ParseFn] = _build_registry()


def get_parser(parser_key: str) -> ParseFn | None:
    return PARSER_REGISTRY.get(parser_key)
