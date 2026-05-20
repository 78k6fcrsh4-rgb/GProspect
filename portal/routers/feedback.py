"""
portal/routers/feedback.py
--------------------------
Feedback endpoints — missed grant submission and learning loop.

Endpoints:
    POST /feedback/submit     — submit a missed grant opportunity
    GET  /feedback/submissions — view submission history (admin only)
    GET  /feedback/submissions/{id} — view single submission

Both Admin and User roles can submit missed grants.
Only Admin can view submission history and learning log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.db import get_db
from portal.auth.dependencies import get_current_user, get_current_admin
from portal.models.user import User

router = APIRouter(prefix="/feedback", tags=["Feedback"])


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────

class MissedGrantRequest(BaseModel):
    """Request body for submitting a missed grant."""
    funder_name:    str
    program_name:   str
    source_url:     str
    deadline:       Optional[str] = None
    award_range:    Optional[str] = None
    eligibility:    Optional[str] = None
    notes:          Optional[str] = None
    funder_website: Optional[str] = None


class FeedbackResponse(BaseModel):
    """Response after processing a missed grant submission."""
    success:        bool
    message:        str
    submission_id:  Optional[str]
    already_found:  bool
    learned:        Optional[str]
    changes_made:   list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/submit", response_model=FeedbackResponse)
def submit_missed_grant(
    request:      MissedGrantRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Submits a grant opportunity the agent missed.

    Triggers the full learning loop:
        1. Validates the submission
        2. Checks if agent already found this grant
        3. Runs gap analysis to identify why it was missed
        4. Updates watch list and search patterns if confident
        5. Logs everything to the learning log
        6. Returns confirmation with what was learned

    Both Admin and User roles can submit.

    Args:
        request:      Missed grant details.
        current_user: Any authenticated user (auto-injected).
        db:           Database session.

    Returns:
        FeedbackResponse with what the agent learned.

    Raises:
        HTTPException 404: Organization profile not found.
        HTTPException 500: Processing error.
    """
    try:
        from agent.profile import OrgProfile
        from learning.feedback import FeedbackProcessor

        profile = _load_profile(current_user.org_name)
        if not profile:
            raise HTTPException(
                status_code = status.HTTP_404_NOT_FOUND,
                detail      = f"No profile found for: {current_user.org_name}",
            )

        processor = FeedbackProcessor(profile)

        result = processor.submit(
            funder_name    = request.funder_name,
            program_name   = request.program_name,
            source_url     = request.source_url,
            submitted_by   = current_user.full_name,
            deadline       = request.deadline,
            award_range    = request.award_range,
            eligibility    = request.eligibility,
            notes          = request.notes,
            funder_website = request.funder_website,
        )

        return FeedbackResponse(
            success       = result["success"],
            message       = result["message"],
            submission_id = result["submission_id"],
            already_found = result["already_found"],
            learned       = result["learned"],
            changes_made  = result["changes_made"],
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = f"Error processing submission: {str(e)}",
        )


@router.get("/submissions")
def get_submissions(
    limit:         int  = 20,
    current_admin: User = Depends(get_current_admin),
):
    """
    Returns recent missed grant submissions (Admin only).

    Shows what staff have submitted and what the agent
    learned from each submission.

    Args:
        limit:         Maximum submissions to return.
        current_admin: Must be Admin (auto-injected).

    Returns:
        List of submission records.
    """
    try:
        from agent.profile import OrgProfile
        from learning.feedback import FeedbackProcessor

        profile = _load_profile(current_admin.org_name)
        if not profile:
            return []

        processor   = FeedbackProcessor(profile)
        submissions = processor.get_submissions(limit=limit)
        return submissions

    except Exception as e:
        print(f"[Feedback] Error loading submissions: {e}")
        return []


@router.get("/submissions/{submission_id}")
def get_submission(
    submission_id: str,
    current_admin: User = Depends(get_current_admin),
):
    """
    Returns a specific submission by ID (Admin only).

    Args:
        submission_id: The unique submission ID.
        current_admin: Must be Admin (auto-injected).

    Returns:
        Submission record or 404 if not found.
    """
    try:
        from agent.profile import OrgProfile
        from learning.feedback import FeedbackProcessor

        profile = _load_profile(current_admin.org_name)
        if not profile:
            raise HTTPException(
                status_code = status.HTTP_404_NOT_FOUND,
                detail      = "Organization profile not found.",
            )

        processor  = FeedbackProcessor(profile)
        submission = processor.get_submission(submission_id)

        if not submission:
            raise HTTPException(
                status_code = status.HTTP_404_NOT_FOUND,
                detail      = f"Submission {submission_id} not found.",
            )

        return submission

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = f"Error loading submission: {str(e)}",
        )


@router.get("/stats")
def get_feedback_stats(
    current_admin: User = Depends(get_current_admin),
):
    """
    Returns learning loop statistics (Admin only).

    Shows total submissions, changes made, sources added,
    and other learning loop metrics.

    Args:
        current_admin: Must be Admin (auto-injected).

    Returns:
        Learning loop statistics dictionary.
    """
    try:
        from agent.profile import OrgProfile
        from learning.learning_log import LearningLog

        profile = _load_profile(current_admin.org_name)
        if not profile:
            return {}

        log   = LearningLog(profile)
        stats = log.get_stats()

        return {
            "learning_loop_stats": stats,
            "message": (
                f"The agent has processed {stats.get('submissions', 0)} "
                f"missed grant submissions and made {stats.get('changes', 0)} "
                f"changes to its own configuration."
            )
        }

    except Exception as e:
        print(f"[Feedback] Error loading stats: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_profile(org_name: str):
    """Loads the org profile for the given organization name."""
    from agent.profile import OrgProfile

    profiles_dir = Path("profiles")
    if not profiles_dir.exists():
        return None

    for profile_file in profiles_dir.glob("*.json"):
        if profile_file.name == "org_profile_template.json":
            continue
        try:
            profile = OrgProfile.from_json(profile_file)
            if profile.org_name.lower() == org_name.lower():
                return profile
        except Exception:
            continue

    return None