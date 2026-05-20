"""
learning/feedback.py
--------------------
FeedbackProcessor — receives and processes missed grant submissions
from staff, triggering the autonomous learning loop.

When a staff member finds a grant the agent missed they submit
it through the portal. This module:
    1. Validates the submission
    2. Checks if the agent already found this grant
    3. Structures it into a MissedGrantSubmission object
    4. Passes it to the GapAnalyzer for processing
    5. Returns a confirmation with what the agent learned

This is the entry point for the entire learning loop.
Every self-improvement the agent makes starts here.

Usage:
    from learning.feedback import FeedbackProcessor
    from agent.profile import OrgProfile

    profile   = OrgProfile.from_json("profiles/deborah_place.json")
    processor = FeedbackProcessor(profile)

    result = processor.submit(
        funder_name   = "Pritzker Traubert Foundation",
        program_name  = "Neighborhood Opportunity Fund",
        source_url    = "https://ptfchicago.org/grants/nof",
        deadline      = "2026-08-15",
        award_range   = "$50,000 - $150,000",
        submitted_by  = "Mary Kelly",
        notes         = "Found this through a partner organization newsletter"
    )

    print(result["message"])
    print(result["learned"])
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from agent.profile import OrgProfile
from agent.state import AgentState
from learning.learning_log import LearningLog


@dataclass
class MissedGrantSubmission:
    """
    Represents a grant opportunity submitted by staff that
    the agent failed to surface on its own.

    This is the standard format all missed grant submissions
    are converted into before being passed to the gap analyzer.
    """
    submission_id:  str
    funder_name:    str
    program_name:   str
    source_url:     str
    submitted_by:   str
    submitted_at:   str
    deadline:       Optional[str]       = None
    award_range:    Optional[str]       = None
    eligibility:    Optional[str]       = None
    notes:          Optional[str]       = None
    funder_website: Optional[str]       = None
    org_name:       str                 = ""
    already_found:  bool                = False
    tags:           list[str]           = field(default_factory=list)

    def to_dict(self) -> dict:
        """Converts submission to a dictionary for storage."""
        return {
            "submission_id":  self.submission_id,
            "funder_name":    self.funder_name,
            "program_name":   self.program_name,
            "source_url":     self.source_url,
            "submitted_by":   self.submitted_by,
            "submitted_at":   self.submitted_at,
            "deadline":       self.deadline,
            "award_range":    self.award_range,
            "eligibility":    self.eligibility,
            "notes":          self.notes,
            "funder_website": self.funder_website,
            "org_name":       self.org_name,
            "already_found":  self.already_found,
            "tags":           self.tags,
        }


class FeedbackProcessor:
    """
    Processes missed grant submissions and triggers the learning loop.

    Validates incoming submissions, checks for duplicates,
    and coordinates with the GapAnalyzer and WatchListUpdater
    to improve the agent's search coverage.
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the feedback processor.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile      = profile
        self.state        = AgentState(profile)
        self.learning_log = LearningLog(profile)

        # Directory for storing submission records
        self.submissions_dir = (
            Path("outputs") /
            self._org_slug() /
            "feedback_submissions"
        )
        self.submissions_dir.mkdir(parents=True, exist_ok=True)

    def submit(
        self,
        funder_name:    str,
        program_name:   str,
        source_url:     str,
        submitted_by:   str,
        deadline:       Optional[str] = None,
        award_range:    Optional[str] = None,
        eligibility:    Optional[str] = None,
        notes:          Optional[str] = None,
        funder_website: Optional[str] = None,
    ) -> dict:
        """
        Processes a missed grant submission from staff.

        This is the main method called by the portal when
        a staff member submits a grant the agent missed.

        Steps:
            1. Validate the submission
            2. Generate a unique submission ID
            3. Check if the agent already found this grant
            4. Save the submission record
            5. Trigger gap analysis
            6. Return confirmation with what was learned

        Args:
            funder_name:    Full name of the funding organization.
            program_name:   Name of the specific grant program.
            source_url:     URL where the grant was found.
            submitted_by:   Name of the staff member submitting.
            deadline:       Application deadline if known.
            award_range:    Award range if known.
            eligibility:    Eligibility requirements if known.
            notes:          Any notes from the staff member.
            funder_website: Funder's main website if different
                           from source_url.

        Returns:
            Dictionary containing:
                - success: bool
                - message: Human-readable confirmation
                - submission_id: Unique ID for this submission
                - already_found: Whether agent already had this
                - learned: What the agent learned from this
                - changes_made: List of changes made to agent
        """
        print(f"\n[FeedbackProcessor] Processing submission from {submitted_by}")
        print(f"[FeedbackProcessor] Grant: {funder_name} — {program_name}")

        # ── Step 1: Validate ──────────────────────────────────────────────────
        validation = self._validate_submission(
            funder_name, program_name, source_url
        )
        if not validation["valid"]:
            return {
                "success":       False,
                "message":       validation["message"],
                "submission_id": None,
                "already_found": False,
                "learned":       None,
                "changes_made":  [],
            }

        # ── Step 2: Generate submission ID ────────────────────────────────────
        submission_id = self._generate_submission_id(
            funder_name, program_name
        )

        # ── Step 3: Check if already found ───────────────────────────────────
        already_found = self._check_already_found(funder_name, program_name)
        if already_found:
            print(f"[FeedbackProcessor] This grant was already in agent results")

        # ── Step 4: Build submission object ──────────────────────────────────
        submission = MissedGrantSubmission(
            submission_id  = submission_id,
            funder_name    = funder_name,
            program_name   = program_name,
            source_url     = source_url,
            submitted_by   = submitted_by,
            submitted_at   = datetime.now().isoformat(),
            deadline       = deadline,
            award_range    = award_range,
            eligibility    = eligibility,
            notes          = notes,
            funder_website = funder_website or source_url,
            org_name       = self.profile.org_name,
            already_found  = already_found,
        )

        # ── Step 5: Save submission record ────────────────────────────────────
        self._save_submission(submission)

        # ── Step 6: Trigger gap analysis if not already found ─────────────────
        learned       = None
        changes_made  = []

        if not already_found:
            print(f"[FeedbackProcessor] Triggering gap analysis...")
            learned, changes_made = self._trigger_gap_analysis(submission)
        else:
            learned = (
                "The agent had already found this grant opportunity. "
                "No changes needed — the agent's coverage is working "
                "correctly for this source."
            )

        # ── Step 7: Log to learning log ───────────────────────────────────────
        self.learning_log.log_submission(
            submission    = submission.to_dict(),
            learned       = learned,
            changes_made  = changes_made,
        )

        # ── Step 8: Build response ────────────────────────────────────────────
        if already_found:
            message = (
                f"Thank you {submitted_by} — this grant was already in "
                f"the agent's results. No changes were needed."
            )
        elif changes_made:
            message = (
                f"Thank you {submitted_by} — the agent has updated itself "
                f"based on your submission. {len(changes_made)} change(s) "
                f"made to improve future search coverage."
            )
        else:
            message = (
                f"Thank you {submitted_by} — your submission has been "
                f"recorded. The agent could not confidently identify the "
                f"source gap and has flagged this for admin review."
            )

        print(f"[FeedbackProcessor] {message}")

        return {
            "success":       True,
            "message":       message,
            "submission_id": submission_id,
            "already_found": already_found,
            "learned":       learned,
            "changes_made":  changes_made,
        }

    def get_submissions(self, limit: int = 20) -> list[dict]:
        """
        Returns recent missed grant submissions.

        Used by the admin portal to display submission history
        and review what the agent has learned.

        Args:
            limit: Maximum number of submissions to return.

        Returns:
            List of submission dictionaries, most recent first.
        """
        submission_files = sorted(
            self.submissions_dir.glob("submission_*.json"),
            reverse = True
        )

        submissions = []
        for f in submission_files[:limit]:
            try:
                with open(f, "r") as file:
                    submissions.append(json.load(file))
            except Exception:
                continue

        return submissions

    def get_submission(self, submission_id: str) -> Optional[dict]:
        """
        Returns a specific submission by ID.

        Args:
            submission_id: The unique submission ID.

        Returns:
            Submission dictionary or None if not found.
        """
        submission_file = (
            self.submissions_dir / f"submission_{submission_id}.json"
        )
        if not submission_file.exists():
            return None

        try:
            with open(submission_file, "r") as f:
                return json.load(f)
        except Exception:
            return None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _validate_submission(
        self,
        funder_name:  str,
        program_name: str,
        source_url:   str
    ) -> dict:
        """
        Validates a submission before processing.

        Args:
            funder_name:  Funder name to validate.
            program_name: Program name to validate.
            source_url:   Source URL to validate.

        Returns:
            Dictionary with valid bool and message.
        """
        if not funder_name or len(funder_name.strip()) < 3:
            return {
                "valid":   False,
                "message": "Funder name is required and must be at least 3 characters."
            }

        if not program_name or len(program_name.strip()) < 3:
            return {
                "valid":   False,
                "message": "Program name is required and must be at least 3 characters."
            }

        if not source_url or not source_url.startswith("http"):
            return {
                "valid":   False,
                "message": "A valid source URL starting with http is required."
            }

        return {"valid": True, "message": "Valid"}

    def _generate_submission_id(
        self,
        funder_name:  str,
        program_name: str
    ) -> str:
        """
        Generates a unique submission ID.

        Args:
            funder_name:  Funder name.
            program_name: Program name.

        Returns:
            Short unique ID string.
        """
        raw = (
            funder_name.lower() +
            program_name.lower() +
            datetime.now().isoformat()
        )
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _check_already_found(
        self,
        funder_name:  str,
        program_name: str
    ) -> bool:
        """
        Checks whether this grant is already in the agent's
        seen opportunities set.

        Args:
            funder_name:  Funder name to check.
            program_name: Program name to check.

        Returns:
            True if already found, False if genuinely missed.
        """
        fingerprint_raw = (
            funder_name.lower().strip() +
            "|" +
            program_name.lower().strip()
        )
        fingerprint = hashlib.md5(
            fingerprint_raw.encode()
        ).hexdigest()

        return fingerprint in self.state.seen_fingerprints

    def _save_submission(self, submission: MissedGrantSubmission) -> None:
        """
        Saves a submission record to disk.

        Args:
            submission: MissedGrantSubmission to save.
        """
        submission_file = (
            self.submissions_dir /
            f"submission_{submission.submission_id}.json"
        )

        with open(submission_file, "w") as f:
            json.dump(submission.to_dict(), f, indent=2)

        print(f"[FeedbackProcessor] Submission saved: {submission_file}")

    def _trigger_gap_analysis(
        self,
        submission: MissedGrantSubmission
    ) -> tuple[str, list[str]]:
        """
        Triggers the gap analyzer to determine why this grant
        was missed and what changes to make.

        Imports GapAnalyzer here to avoid circular imports.

        Args:
            submission: The validated submission to analyze.

        Returns:
            Tuple of (learned string, list of changes made).
        """
        try:
            from learning.gap_analyzer import GapAnalyzer
            from learning.watch_list_updater import WatchListUpdater

            analyzer = GapAnalyzer(self.profile)
            analysis = analyzer.analyze(submission)

            if analysis["confidence"] == "high":
                updater  = WatchListUpdater(self.profile)
                changes  = updater.apply_changes(analysis)
                learned  = analysis["explanation"]
                return learned, changes
            else:
                # Low confidence — log for admin review
                learned = (
                    f"Gap identified but confidence is low. "
                    f"Analysis: {analysis['explanation']} "
                    f"Flagged for admin review before making changes."
                )
                return learned, []

        except Exception as e:
            print(f"[FeedbackProcessor] Gap analysis error: {e}")
            return f"Gap analysis encountered an error: {e}", []

    def _org_slug(self) -> str:
        """Returns a filesystem-safe org name slug."""
        return (
            self.profile.org_short_name
            .lower()
            .replace(" ", "_")
            .replace("'", "")
        )