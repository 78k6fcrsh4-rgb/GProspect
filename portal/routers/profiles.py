"""
portal/routers/profiles.py
--------------------------
Profile-version endpoints for the Phase 1a intake wizard.

  GET  /orgs/me/profile/current   — return the current OrgProfileVersion
  POST /orgs/me/profile/version   — save a new version (validated)
  GET  /orgs/me/profile/history   — list every version (no payload)
  POST /orgs/me/profile/extract   — doc-assist: upload a doc, get fields

All endpoints are scoped to current_user.org_id — there is no path or
body parameter accepting an org_id, so cross-org access is impossible
by construction. /version is admin-only; the others are open to any
authenticated user in the org.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.intake             import (
    UnsupportedFileType,
    extract_profile_fields_from_text,
    extract_text_from_upload,
)
from agent.profile            import OrgProfile
from database.db              import get_db
from portal.auth.dependencies import get_current_admin, get_current_user
from portal.models.org_profile import (
    OrgProfileVersion,
    create_next_version,
    get_current_for_org,
)
from portal.models.user        import User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/orgs/me/profile", tags=["Organization Profile"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class ProfileVersionSummary(BaseModel):
    """Lightweight version metadata — used by the history endpoint."""
    id:                 int
    version:            int
    is_current:         bool
    created_by_user_id: int | None
    created_at:         str | None


class ProfileVersionFull(ProfileVersionSummary):
    """Includes the payload — used by current + post-save responses."""
    payload: dict[str, Any]


class SaveProfileRequest(BaseModel):
    """
    Payload sent by the intake wizard on Save & Submit.

    `profile` is the full OrgProfile JSON. The endpoint validates it
    against the Pydantic OrgProfile model — invalid payloads return 422
    with structured field-level errors the wizard can surface back to
    the user.
    """
    profile: dict[str, Any]


class ExtractResponse(BaseModel):
    """Doc-assist response — the fields Claude could populate from the doc."""
    extracted_fields: dict[str, Any]
    notes:            list[str]  # human-readable notes (e.g. "could not extract budget")


# ─────────────────────────────────────────────────────────────────────────────
# GET /orgs/me/profile/current
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/current", response_model=ProfileVersionFull)
def get_current_profile(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Returns the active profile version for the calling user's org.

    Used by the intake wizard on load to bootstrap with existing data
    (so an org editing its profile doesn't start from a blank form).

    Args:
        current_user: Authenticated user (auto-injected).
        db:           Database session.

    Returns:
        ProfileVersionFull with version metadata + the full payload.

    Raises:
        HTTPException 404: No profile version exists for this org yet.
    """
    version = get_current_for_org(db, current_user.org_id)
    if version is None:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "No profile saved yet for this organization.",
        )
    return _to_full(version)


# ─────────────────────────────────────────────────────────────────────────────
# GET /orgs/me/profile/history
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/history", response_model=list[ProfileVersionSummary])
def get_profile_history(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Returns every version saved for the calling user's org, newest first.

    Used by the "Profile history" panel in Phase 1a (P1 nice-to-have).
    Payload is intentionally omitted to keep the response small — the
    UI fetches the full payload only when a user expands a row.
    """
    versions = (
        db.query(OrgProfileVersion)
          .filter(OrgProfileVersion.org_id == current_user.org_id)
          .order_by(OrgProfileVersion.version.desc())
          .all()
    )
    return [_to_summary(v) for v in versions]


# ─────────────────────────────────────────────────────────────────────────────
# POST /orgs/me/profile/version
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/version", response_model=ProfileVersionFull,
             status_code=status.HTTP_201_CREATED)
def save_profile_version(
    request:       SaveProfileRequest,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    """
    Save a new profile version for the calling admin's org.

    Validates the payload against the Pydantic OrgProfile schema before
    persisting. Validation failures return 422 with the per-field errors
    Pydantic produces — the wizard surfaces those next to the offending
    field.

    On success: creates a new OrgProfileVersion row, marks it is_current,
    flips the prior is_current=True row (if any) to False, and returns
    the new version with its assigned version number.

    Admin-only. Regular users can read the current profile but cannot
    change it.

    Args:
        request:        Wrapped profile JSON.
        current_admin:  Must be Admin (auto-injected).
        db:             Database session.

    Returns:
        ProfileVersionFull representing the newly-created version.

    Raises:
        HTTPException 422: Profile failed Pydantic validation.
    """
    # 1. Validate the payload against the OrgProfile Pydantic schema.
    #    Pydantic's ValidationError → HTTPException 422 with field errors.
    try:
        validated = OrgProfile.model_validate(request.profile)
    except Exception as e:
        # Pydantic v2 ValidationError exposes .errors() but we'd rather
        # keep the message human-readable. Surface str(e) which Pydantic
        # formats nicely.
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail      = f"Profile validation failed: {e}",
        )

    # 2. Persist via the helper so is_current is preserved.
    new_version = create_next_version(
        db,
        org_id             = current_admin.org_id,
        payload            = validated.model_dump(mode="json"),
        created_by_user_id = current_admin.id,
    )
    db.commit()
    db.refresh(new_version)
    log.info(
        "Profile version %d saved for org_id=%s by %s",
        new_version.version, current_admin.org_id, current_admin.email,
    )
    return _to_full(new_version)


# ─────────────────────────────────────────────────────────────────────────────
# POST /orgs/me/profile/extract
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/extract", response_model=ExtractResponse)
async def extract_profile_from_doc(
    file:         UploadFile = File(...),
    current_user: User       = Depends(get_current_user),
):
    """
    Doc-assist endpoint — accepts a .docx/.pdf/.txt/.md upload, runs it
    through Claude, returns the structured fields Claude could extract.

    The response is intentionally permissive: every field is optional;
    fields Claude couldn't extract are omitted. The wizard treats the
    response as a *prefill suggestion*, not a binding answer. Validation
    happens at /version time when the user clicks Save.

    Both Admin and User roles can use this — it doesn't write any data.

    Args:
        file:         The uploaded document.
        current_user: Authenticated user (auto-injected).

    Returns:
        ExtractResponse with extracted_fields (the prefill payload) and
        notes (a list of human-readable observations such as "we
        couldn't identify a budget — please fill in manually").

    Raises:
        HTTPException 400: Unsupported file type or file too large.
    """
    file_bytes = await file.read()

    # Extract text
    try:
        text = extract_text_from_upload(file_bytes, file.filename or "")
    except UnsupportedFileType as e:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = str(e),
        )
    except ValueError as e:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = str(e),
        )

    if not text.strip():
        return ExtractResponse(
            extracted_fields = {},
            notes            = ["The uploaded document contained no readable text."],
        )

    # Hit Claude
    extracted = extract_profile_fields_from_text(text)

    # Build human-readable notes for the wizard to display.
    notes = _build_extraction_notes(extracted)

    return ExtractResponse(
        extracted_fields = extracted,
        notes            = notes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_summary(v: OrgProfileVersion) -> ProfileVersionSummary:
    return ProfileVersionSummary(
        id                 = v.id,
        version            = v.version,
        is_current         = v.is_current,
        created_by_user_id = v.created_by_user_id,
        created_at         = v.created_at.isoformat() if v.created_at else None,
    )


def _to_full(v: OrgProfileVersion) -> ProfileVersionFull:
    return ProfileVersionFull(
        id                 = v.id,
        version            = v.version,
        is_current         = v.is_current,
        created_by_user_id = v.created_by_user_id,
        created_at         = v.created_at.isoformat() if v.created_at else None,
        payload            = v.payload or {},
    )


REQUIRED_FIELDS = ("org_name", "mission_statement", "program_areas",
                   "populations_served", "geography", "budget")


def _build_extraction_notes(extracted: dict) -> list[str]:
    """
    Turn the raw extraction into a list of human-readable observations
    the wizard can show: which required fields are missing, which
    optional-but-helpful fields the user should consider filling in.
    Keep it short — the wizard renders these as bullet points.
    """
    notes: list[str] = []
    if not extracted:
        notes.append(
            "We couldn't extract a profile from this document. "
            "Try a more recent case statement or strategic plan, or fill "
            "in the form manually."
        )
        return notes

    missing_required = [f for f in REQUIRED_FIELDS if not extracted.get(f)]
    if missing_required:
        notes.append(
            "Couldn't extract these required fields — please fill them "
            "in manually: " + ", ".join(missing_required)
        )

    if not extracted.get("ntee_codes"):
        notes.append(
            "No NTEE codes were found. Adding them improves funder matching."
        )
    if not extracted.get("known_funders"):
        notes.append(
            "No prior funders were extracted. Adding them flags currently-"
            "open opportunities from those funders as warm leads."
        )

    return notes
