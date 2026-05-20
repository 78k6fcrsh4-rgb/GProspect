"""
learning/learning_log.py
------------------------
LearningLog — permanent audit trail of every change the agent
makes to its own configuration through the learning loop.

Records:
    - Missed grant submissions from staff
    - Gap analysis results
    - Changes made to the watch list
    - Keywords identified for expansion
    - Recommendations flagged for admin review

The log is stored as individual JSON files in the outputs
directory. Every entry is permanent — nothing is ever deleted.
Admins can review the full change history through the portal.

Usage:
    from learning.learning_log import LearningLog
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    log     = LearningLog(profile)

    log.log_submission(submission_dict, learned, changes)
    log.log_change(change_type, description, triggered_by, details)

    recent = log.get_recent_changes(limit=10)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from agent.profile import OrgProfile


class LearningLog:
    """
    Permanent audit trail for all agent self-modifications.

    Stores two types of entries:
        1. Submission entries — when staff submit missed grants
        2. Change entries — when the agent modifies its config

    All entries are written as individual JSON files so the
    log never gets corrupted by a failed write operation.
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the learning log.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile  = profile
        self.log_dir  = (
            Path("outputs") /
            self._org_slug() /
            "learning_log"
        )
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def log_submission(
        self,
        submission:   dict,
        learned:      Optional[str] = None,
        changes_made: Optional[list] = None,
    ) -> str:
        """
        Logs a missed grant submission and what the agent
        learned from it.

        Args:
            submission:   Submission dictionary from FeedbackProcessor.
            learned:      Plain-English description of what was learned.
            changes_made: List of changes made as a result.

        Returns:
            Path to the log entry file.
        """
        entry = {
            "entry_type":   "submission",
            "timestamp":    datetime.now().isoformat(),
            "org_name":     self.profile.org_name,
            "submission":   submission,
            "learned":      learned or "No analysis performed.",
            "changes_made": changes_made or [],
            "entry_id":     self._generate_entry_id("sub"),
        }

        path = self._write_entry(entry)
        print(f"[LearningLog] Submission logged: {path}")
        return path

    def log_change(
        self,
        change_type:  str,
        description:  str,
        triggered_by: str,
        details:      Optional[dict] = None,
    ) -> str:
        """
        Logs a specific change made to the agent's configuration.

        Args:
            change_type:  Type of change e.g. source_added,
                         keywords_identified, recommendation_logged.
            description:  Plain-English description of the change.
            triggered_by: What triggered this change e.g.
                         gap_analyzer, manual_submission, admin.
            details:      Additional structured details about
                         the change for admin review.

        Returns:
            Path to the log entry file.
        """
        entry = {
            "entry_type":   "change",
            "timestamp":    datetime.now().isoformat(),
            "org_name":     self.profile.org_name,
            "change_type":  change_type,
            "description":  description,
            "triggered_by": triggered_by,
            "details":      details or {},
            "entry_id":     self._generate_entry_id("chg"),
        }

        path = self._write_entry(entry)
        print(f"[LearningLog] Change logged: {change_type} — {description[:60]}")
        return path

    def log_gap_analysis(
        self,
        submission_id: str,
        analysis:      dict,
    ) -> str:
        """
        Logs the full gap analysis result for a submission.

        Args:
            submission_id: ID of the submission being analyzed.
            analysis:      Full analysis dictionary from GapAnalyzer.

        Returns:
            Path to the log entry file.
        """
        entry = {
            "entry_type":    "gap_analysis",
            "timestamp":     datetime.now().isoformat(),
            "org_name":      self.profile.org_name,
            "submission_id": submission_id,
            "analysis":      analysis,
            "entry_id":      self._generate_entry_id("gap"),
        }

        path = self._write_entry(entry)
        print(f"[LearningLog] Gap analysis logged for submission: {submission_id}")
        return path

    def get_recent_changes(self, limit: int = 20) -> list[dict]:
        """
        Returns the most recent log entries across all types.

        Used by the admin portal to display the full learning
        history and what the agent has changed about itself.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of log entry dictionaries, most recent first.
        """
        all_entries = []

        for entry_file in sorted(
            self.log_dir.glob("*.json"),
            reverse = True
        )[:limit * 2]:
            try:
                with open(entry_file, "r", encoding="utf-8") as f:
                    all_entries.append(json.load(f))
            except Exception:
                continue

        # Sort by timestamp descending
        all_entries.sort(
            key     = lambda x: x.get("timestamp", ""),
            reverse = True
        )

        return all_entries[:limit]

    def get_changes_by_type(self, change_type: str) -> list[dict]:
        """
        Returns all log entries of a specific change type.

        Args:
            change_type: Type to filter by e.g. source_added.

        Returns:
            List of matching log entries.
        """
        all_entries = self.get_recent_changes(limit=100)
        return [
            e for e in all_entries
            if e.get("change_type") == change_type
            or e.get("entry_type") == change_type
        ]

    def get_submissions(self, limit: int = 20) -> list[dict]:
        """
        Returns recent submission log entries only.

        Args:
            limit: Maximum number to return.

        Returns:
            List of submission log entries.
        """
        all_entries = self.get_recent_changes(limit=100)
        return [
            e for e in all_entries
            if e.get("entry_type") == "submission"
        ][:limit]

    def get_stats(self) -> dict:
        """
        Returns summary statistics about the learning log.

        Used by the admin portal dashboard to show how much
        the agent has learned over time.

        Returns:
            Dictionary with counts by entry type.
        """
        all_entries    = self.get_recent_changes(limit=500)
        submissions    = sum(1 for e in all_entries if e.get("entry_type") == "submission")
        changes        = sum(1 for e in all_entries if e.get("entry_type") == "change")
        sources_added  = sum(
            1 for e in all_entries
            if e.get("change_type") == "source_added"
        )
        analyses       = sum(1 for e in all_entries if e.get("entry_type") == "gap_analysis")

        return {
            "total_entries":    len(all_entries),
            "submissions":      submissions,
            "changes":          changes,
            "gap_analyses":     analyses,
            "sources_added":    sources_added,
            "log_directory":    str(self.log_dir),
        }

    def print_status(self) -> None:
        """
        Prints a human-readable summary of the learning log.
        """
        stats = self.get_stats()
        print(f"\n{'='*50}")
        print(f"Learning Log — {self.profile.org_short_name}")
        print(f"{'='*50}")
        print(f"  Total entries:    {stats['total_entries']}")
        print(f"  Submissions:      {stats['submissions']}")
        print(f"  Changes logged:   {stats['changes']}")
        print(f"  Sources added:    {stats['sources_added']}")
        print(f"  Gap analyses:     {stats['gap_analyses']}")
        print(f"  Log directory:    {stats['log_directory']}")
        print(f"{'='*50}\n")

    # ── Private helpers ───────────────────────────────────────────────────────

    def _write_entry(self, entry: dict) -> str:
        """
        Writes a log entry to disk as a JSON file.

        Each entry gets its own file so a failed write never
        corrupts existing entries.

        Args:
            entry: The log entry dictionary to write.

        Returns:
            Path to the written file as a string.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        entry_type = entry.get("entry_type", "entry")
        filename   = f"{entry_type}_{timestamp}.json"
        filepath   = self.log_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)

        return str(filepath)

    def _generate_entry_id(self, prefix: str) -> str:
        """
        Generates a short unique entry ID.

        Args:
            prefix: Short prefix e.g. sub, chg, gap.

        Returns:
            Short unique ID string.
        """
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return f"{prefix}_{timestamp}"

    def _org_slug(self) -> str:
        """Returns a filesystem-safe org name slug."""
        return (
            self.profile.org_short_name
            .lower()
            .replace(" ", "_")
            .replace("'", "")
        )