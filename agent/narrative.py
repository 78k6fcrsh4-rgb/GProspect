"""
agent/narrative.py
------------------
Claude-backed narrative generator for the Phase 1b prospect cards.

Each opportunity card shows two complementary takes on the same data:

  conversational_md  — A 3-5 sentence paragraph in development-officer
                       voice. Grounded in the org profile and the
                       opportunity row. Renders on the expanded card
                       by default.

  scored_breakdown   — Per-dimension scores with one-line reasons
                       (geographic, population, programs, budget,
                       timeline). Renders inside a "Show details"
                       expander.

Both are produced in a single Claude call so we don't pay two prompts of
profile/opp context per opportunity. The output is JSON the router can
persist directly into opportunity_narratives.

The actual call is wrapped so tests can stub it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


@dataclass
class NarrativeResult:
    conversational_md: str
    scored_breakdown:  dict
    model_used:        Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a senior development officer writing internal grant prospecting \
notes for a nonprofit ED. You read an organizational profile and a single \
grant opportunity, then produce TWO things:

  1. A 3-5 sentence "why this fits" paragraph in development-officer voice.
     Grounded in the org's actual mission, programs, populations, and
     geography. Specific, not generic — if the funder has prior work that
     overlaps with this org, name it. If the award range is a good fit,
     say so with the numbers. No hype, no fluff.

  2. A per-dimension scored breakdown with brief reasons:
        geographic   (0.0-1.0)
        population   (0.0-1.0)
        programs     (0.0-1.0)
        budget       (0.0-1.0)
        timeline     (0.0-1.0)
     Reasons must be one short clause each — no full sentences.

Return ONLY JSON in the exact schema below. No prose, no fences, no
explanation outside the JSON.

Schema:
{
  "conversational_md": "<the 3-5 sentence paragraph as markdown>",
  "scored_breakdown": {
    "geographic": {"score": 0.0, "reason": "..."},
    "population": {"score": 0.0, "reason": "..."},
    "programs":   {"score": 0.0, "reason": "..."},
    "budget":     {"score": 0.0, "reason": "..."},
    "timeline":   {"score": 0.0, "reason": "..."}
  }
}

If you can't form an opinion for a given dimension (data missing), use
score=0.0 and reason="insufficient data". Never hallucinate facts about
the funder. If the opportunity row doesn't tell you the funder's prior
giving, don't invent it — keep the paragraph grounded in what's known.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_narrative(
    profile_payload:  dict,
    opportunity_row:  dict,
    anthropic_client = None,
    model:            str  = DEFAULT_MODEL,
) -> NarrativeResult:
    """
    Run Claude over (profile, opportunity) and return the narrative pair.

    Args:
        profile_payload:  The OrgProfile JSON payload (from
                          OrgProfileVersion.payload).
        opportunity_row:  A dict-shaped CSV row for the opportunity.
        anthropic_client: Pre-constructed Anthropic client. If None, one is
                          built from ANTHROPIC_API_KEY in the env.
        model:            Override the model name.

    Returns:
        NarrativeResult with conversational_md, scored_breakdown, and the
        model name used. On any failure (missing API key, JSON parse error,
        API error), returns a graceful fallback rather than raising — the
        caller persists the fallback so the user sees *something* and doesn't
        get a hard 500 from an opportunity that can't be narrated.
    """
    if anthropic_client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            log.warning(
                "generate_narrative: ANTHROPIC_API_KEY not set — returning "
                "fallback narrative."
            )
            return _fallback_narrative(opportunity_row)
        try:
            import anthropic
            anthropic_client = anthropic.Anthropic(api_key=api_key)
        except Exception:
            log.exception("Failed to construct Anthropic client")
            return _fallback_narrative(opportunity_row)

    user_message = (
        "Organization profile:\n"
        f"```json\n{json.dumps(profile_payload, indent=2)[:8000]}\n```\n\n"
        "Grant opportunity (one row from the prospect list):\n"
        f"```json\n{json.dumps(opportunity_row, indent=2)[:4000]}\n```\n\n"
        "Return the JSON object now — no commentary, no fences."
    )

    try:
        response = anthropic_client.messages.create(
            model       = model,
            max_tokens  = 1024,
            system      = _SYSTEM_PROMPT,
            messages    = [{"role": "user", "content": user_message}],
        )
        raw = _first_text_block(response)
        if not raw:
            log.warning("Claude returned no text for narrative generation")
            return _fallback_narrative(opportunity_row, model_used=model)

        parsed = _parse_narrative_json(raw)
        if parsed is None:
            log.warning("Could not parse narrative JSON: %r", raw[:200])
            return _fallback_narrative(opportunity_row, model_used=model)

        return NarrativeResult(
            conversational_md = parsed.get("conversational_md", "") or
                                _fallback_paragraph(opportunity_row),
            scored_breakdown  = parsed.get("scored_breakdown", {}) or {},
            model_used        = model,
        )

    except Exception:
        log.exception("Claude narrative call failed")
        return _fallback_narrative(opportunity_row, model_used=model)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _first_text_block(response) -> str:
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _parse_narrative_json(raw: str) -> Optional[dict]:
    """Strip ```json fences if Claude added them, parse, return dict or None."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    first = s.find("{")
    last  = s.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None
    try:
        return json.loads(s[first : last + 1])
    except json.JSONDecodeError:
        return None


def _fallback_paragraph(opportunity_row: dict) -> str:
    """
    Generic but honest fallback — used when Claude is unavailable, the API
    key is missing, or the response can't be parsed. Never hallucinates;
    just summarizes what's already known from the CSV.
    """
    funder  = opportunity_row.get("funder_name")  or "This funder"
    program = opportunity_row.get("program_name") or "this program"
    award   = opportunity_row.get("award_range")  or ""
    deadline= opportunity_row.get("application_deadline") or ""
    bits = [f"{funder} — {program}."]
    if award:
        bits.append(f"Award range: {award}.")
    if deadline:
        bits.append(f"Deadline: {deadline}.")
    bits.append(
        "AI narrative unavailable — sign in to the portal and refresh once "
        "the API key is configured, or fill in details manually."
    )
    return " ".join(bits)


def _fallback_narrative(
    opportunity_row: dict,
    model_used: Optional[str] = None,
) -> NarrativeResult:
    """Generic NarrativeResult with empty score breakdown."""
    return NarrativeResult(
        conversational_md = _fallback_paragraph(opportunity_row),
        scored_breakdown  = {
            "geographic": {"score": 0.0, "reason": "insufficient data"},
            "population": {"score": 0.0, "reason": "insufficient data"},
            "programs":   {"score": 0.0, "reason": "insufficient data"},
            "budget":     {"score": 0.0, "reason": "insufficient data"},
            "timeline":   {"score": 0.0, "reason": "insufficient data"},
        },
        model_used = model_used,
    )
