"""
portal/routers/digests.py
-------------------------
Weekly-digest export endpoint (Phase 1b).

  POST /digests/generate   → returns a ZIP with both .docx and .pdf

The ZIP keeps the response a single download — the frontend offers one
button, the user gets one file, and inside are both formats. PDF goes
to anyone who needs to forward; DOCX goes to anyone who wants to edit.

The endpoint:
  1. Pulls the org's current profile + opportunity list (via the same
     CSV-backed loader the /opportunities router uses).
  2. Pulls cached narratives for whichever opportunities have them.
     Does NOT trigger fresh Claude calls — narratives are generated
     lazily on card-expand in the UI; the digest uses whatever cache
     already exists. (This caps the cost of digest generation.)
  3. Builds the payload via agent.digest.build_digest_payload.
  4. Renders both .docx and .pdf.
  5. Zips them up and streams the ZIP back.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from agent.digest         import build_digest_payload, render_docx, render_pdf
from agent.opportunities  import compute_opportunity_key, parse_days_remaining, classify_deadline
from database.db          import get_db
from portal.auth.dependencies      import get_current_user
from portal.models.opportunity     import OpportunityNarrative
from portal.models.org_profile     import get_current_for_org
from portal.models.user            import User
from portal.routers.results        import _load_latest_results

log = logging.getLogger(__name__)
router = APIRouter(prefix="/digests", tags=["Digests"])


@router.post("/generate")
def generate_digest(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Build the weekly digest for the calling org and return a ZIP
    containing both the .docx and .pdf versions.

    Pre-conditions:
      - Org has a saved profile (any version).
      - Org has at least one row in the latest grant_prospects CSV.

    Returns:
        application/zip with weekly_digest.docx + weekly_digest.pdf.

    Raises:
        HTTPException 400: No saved profile yet (can't generate without
                           the org-context line in the header).
        HTTPException 404: No opportunities — nothing to digest. Easier
                           for the frontend to surface this as a
                           specific empty state than a generic 200 with
                           an empty PDF.
    """
    current_profile = get_current_for_org(db, current_user.org_id)
    if current_profile is None:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = "Save your organization profile in Intake before "
                          "generating the digest.",
        )

    raw_rows = _load_latest_results(current_user.org_name) or []
    if not raw_rows:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "No opportunities to digest. Trigger an agent "
                          "run first.",
        )

    # Enrich rows with rank / opp_key / days_remaining / bucket
    enriched: list[dict] = []
    for idx, r in enumerate(raw_rows):
        funder  = (r.get("funder_name")  or "").strip()
        program = (r.get("program_name") or "").strip()
        if not funder and not program:
            continue
        opp_key = compute_opportunity_key(funder, program)
        days    = parse_days_remaining(r.get("days_remaining"))
        enriched.append({
            **r,
            "rank":            idx + 1,
            "opp_key":         opp_key,
            "funder_name":     funder,
            "program_name":    program,
            "days_remaining":  days,
            "bucket":          classify_deadline(days),
            "score_final":     _safe_float(r.get("score_final")),
        })

    # Pull whatever narratives are already cached at the current profile
    # version. Does NOT trigger fresh Claude calls.
    opp_keys = [o["opp_key"] for o in enriched]
    cached = (
        db.query(OpportunityNarrative)
          .filter(OpportunityNarrative.org_id          == current_user.org_id,
                  OpportunityNarrative.opp_key.in_(opp_keys) if opp_keys else False,
                  OpportunityNarrative.profile_version == current_profile.version)
          .all()
    )
    narratives = {n.opp_key: n.conversational_md for n in cached}

    org_display_name = current_user.org_name  # display name, denormalized on user row

    payload = build_digest_payload(
        org_display_name = org_display_name,
        opportunities    = enriched,
        profile_payload  = current_profile.payload,
        narratives       = narratives,
        generated_at     = datetime.now(timezone.utc),
    )

    # Render both formats
    try:
        docx_bytes = render_docx(payload)
        pdf_bytes  = render_pdf(payload)
    except Exception as e:
        log.exception("Digest render failed for org_id=%s", current_user.org_id)
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = f"Digest render failed: {e}",
        )

    # Zip them
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("weekly_digest.docx", docx_bytes)
        zf.writestr("weekly_digest.pdf",  pdf_bytes)
    zip_buf.seek(0)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"gprospect_digest_{today}.zip"

    return StreamingResponse(
        iter([zip_buf.getvalue()]),
        media_type = "application/zip",
        headers    = {
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _safe_float(val):
    try:
        if val is None or val == "" or val == "Not scored":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None
