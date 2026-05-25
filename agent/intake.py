"""
agent/intake.py
---------------
Doc-assist for the Phase 1a profile intake wizard.

Two pieces:

  1. `extract_text_from_upload(file_bytes, filename) -> str`
     Dispatches on extension. Supports .docx (mammoth), .pdf (pypdf),
     .txt, .md. Returns the document's plain text, defensively handling
     extraction failures (returns partial text or a clear error string
     rather than raising — the caller decides whether to surface).

  2. `extract_profile_fields_from_text(text, anthropic_client=None) -> dict`
     Sends the extracted text + an inlined description of the OrgProfile
     schema to Claude and returns the populated fields as a dict the
     frontend can drop into the wizard's session state.

Both functions are pure and stateless so they're easy to test. The router
in portal/routers/profiles.py is a thin wrapper that handles the FastAPI
upload boilerplate and the API key plumbing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from io import BytesIO
from typing import Optional

log = logging.getLogger(__name__)


# Maximum size of an uploaded document. Beyond this we refuse — large
# documents both inflate the API bill and rarely contain a coherent
# org profile (book-length grant boilerplate is mostly noise).
MAX_UPLOAD_BYTES = 5 * 1024 * 1024   # 5 MB

# Maximum characters of extracted text fed to Claude. Claude has a much
# larger context window than this but the marginal value of more text
# drops sharply after ~30k characters for org-profile extraction.
MAX_EXTRACTION_CHARS = 60_000


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction — dispatch on extension
# ─────────────────────────────────────────────────────────────────────────────

class UnsupportedFileType(ValueError):
    """Raised when a file extension isn't handled by extract_text_from_upload."""


def extract_text_from_upload(file_bytes: bytes, filename: str) -> str:
    """
    Extract plain text from an uploaded document.

    Args:
        file_bytes: Raw bytes of the uploaded file.
        filename:   The original filename (used only for the extension).

    Returns:
        The document's plain text. Trimmed and de-newlined a little so it's
        easier for Claude to read.

    Raises:
        UnsupportedFileType: If the extension isn't one we handle.
        ValueError: If the file is too large.
    """
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"File too large ({len(file_bytes):,} bytes). "
            f"Maximum is {MAX_UPLOAD_BYTES:,} bytes."
        )

    ext = _ext(filename)

    if ext in (".txt", ".md", ""):
        return _normalize(file_bytes.decode("utf-8", errors="replace"))
    if ext == ".docx":
        return _normalize(_extract_docx(file_bytes))
    if ext == ".pdf":
        return _normalize(_extract_pdf(file_bytes))

    raise UnsupportedFileType(
        f"Unsupported file extension: {ext or '(none)'}. "
        f"Supported: .docx, .pdf, .txt, .md"
    )


def _ext(filename: str) -> str:
    """Return the lowercased extension including the dot, or '' if none."""
    name = (filename or "").strip().lower()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[1]


def _normalize(text: str) -> str:
    """Collapse runs of blank lines and trailing whitespace. Cap length."""
    text = re.sub(r"[ \t]+\n", "\n", text)        # trailing spaces
    text = re.sub(r"\n{3,}", "\n\n", text)        # 3+ blank lines → 1
    text = text.strip()
    if len(text) > MAX_EXTRACTION_CHARS:
        text = text[:MAX_EXTRACTION_CHARS] + "\n\n[... truncated ...]"
    return text


def _extract_docx(file_bytes: bytes) -> str:
    """Extract text from a .docx file using mammoth. Lazy import."""
    import mammoth
    with BytesIO(file_bytes) as buf:
        result = mammoth.extract_raw_text(buf)
    return result.value or ""


def _extract_pdf(file_bytes: bytes) -> str:
    """Extract text from a .pdf file using pypdf. Lazy import. Best-effort."""
    from pypdf import PdfReader
    with BytesIO(file_bytes) as buf:
        reader = PdfReader(buf)
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as e:
                # Some PDF pages have malformed content streams; skip and continue.
                log.warning("PDF page extraction failed: %s", e)
                pages.append("")
    return "\n\n".join(pages)


# ─────────────────────────────────────────────────────────────────────────────
# Claude-backed structured extraction
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = """\
You extract structured organizational-profile fields from nonprofit \
documents (case statements, strategic plans, 990 narratives, annual \
reports). You return ONLY a JSON object — never any prose, never any \
explanation. Match the schema described below as closely as the document \
allows.

Rules:
  - Only populate a field if you can extract it confidently from the
    document. When unsure, OMIT the field entirely (do NOT guess).
  - Return null/empty for fields the document does not mention.
  - For enum-valued fields (program_areas, populations_served,
    funder_type), use ONLY the exact values listed in the schema. If
    none of the document's language maps cleanly to an enum value,
    omit the field.
  - For mission_statement, prefer the org's stated mission verbatim
    if present. Don't paraphrase.
  - All currency values are integer USD (e.g. 250000 not "$250,000").
  - EIN must match the format XX-XXXXXXX. If you can't find one, omit.
"""


_EXTRACT_SCHEMA_BRIEF = """\
The JSON object should match the OrgProfile schema. Top-level fields:

  org_name           (string, required if extractable)
  org_short_name     (string)
  ein                (string, format XX-XXXXXXX)
  ntee_codes         (array of strings like "L41", "P20")
  website            (string URL)
  founded_year       (integer)
  mission_statement  (string, ≥20 chars)
  mission_keywords   (array of strings)
  program_areas      (array; enum values: housing_permanent, housing_transitional,
                      housing_rapid_rehousing, domestic_violence,
                      workforce_development, food_security, healthcare,
                      mental_health, substance_use, reentry, legal_services,
                      childcare, education, financial_literacy, general_operating)
  program_descriptions (object: {program_name: description})
  populations_served (array; enum values: women, men, youth, seniors, families,
                      veterans, lgbtq, immigrants, formerly_incarcerated,
                      survivors_dv, chronically_homeless, low_income, disabled,
                      bipoc, mental_health)
  geography          (object with city (string), state (2-letter), county,
                      region, national (bool))
  budget             (object with request_floor (int USD), request_ceiling
                      (int USD), annual_budget (int USD))
  known_funders      (array of {name, last_award_year, last_award_amount,
                      funder_type, notes})

If a field is mentioned but its value isn't clear enough to populate
confidently, omit it. Never invent values.
"""


def extract_profile_fields_from_text(
    text:              str,
    anthropic_client = None,
    model:             str = "claude-sonnet-4-5-20250929",
) -> dict:
    """
    Use Claude to extract OrgProfile-shaped fields from a document.

    Args:
        text:             The plain text of the uploaded document.
        anthropic_client: An Anthropic SDK client. If None, one is built
                          from ANTHROPIC_API_KEY in the environment.
        model:            Override the model name.

    Returns:
        A dict with whatever fields Claude could extract. Empty dict on
        failure (logged at warning level — never raises). The caller is
        responsible for validating against the Pydantic OrgProfile model
        if/when the user submits the wizard.
    """
    if not text or not text.strip():
        return {}

    if anthropic_client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            log.warning(
                "extract_profile_fields_from_text: ANTHROPIC_API_KEY not "
                "set — returning empty extraction. The wizard will start "
                "from an empty profile, which is the safe fallback."
            )
            return {}
        try:
            import anthropic
            anthropic_client = anthropic.Anthropic(api_key=api_key)
        except Exception:
            log.exception("Failed to construct Anthropic client")
            return {}

    user_msg = (
        f"{_EXTRACT_SCHEMA_BRIEF}\n\n"
        f"Document to extract from:\n\n"
        f"<<<DOC\n{text}\n>>>DOC\n\n"
        f"Return ONLY the JSON object — no preamble, no fences, no commentary."
    )

    try:
        response = anthropic_client.messages.create(
            model      = model,
            max_tokens = 4096,
            system     = _EXTRACT_SYSTEM_PROMPT,
            messages   = [{"role": "user", "content": user_msg}],
        )
        # Response content is a list of blocks; we want the first text block.
        raw = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                raw = block.text
                break
        if not raw:
            log.warning("Claude returned no text content for extraction")
            return {}

        return _parse_json_lenient(raw)

    except Exception:
        log.exception("Claude extraction failed")
        return {}


def _parse_json_lenient(raw: str) -> dict:
    """
    Parse the first JSON object found in `raw`.

    Claude is instructed to return only JSON but occasionally wraps the
    output in ```json fences or adds a stray sentence. This helper strips
    that and parses what's inside.
    """
    s = raw.strip()
    # Strip fences if present.
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    # Find first { and last } and slice between them — robust to a stray
    # leading word.
    first = s.find("{")
    last  = s.rfind("}")
    if first == -1 or last == -1 or last < first:
        log.warning("No JSON object found in extraction response: %r", s[:200])
        return {}
    s = s[first : last + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        log.warning("Could not parse extraction JSON: %s — head=%r", e, s[:200])
        return {}
