"""
learning/watch_list_updater.py
------------------------------
WatchListUpdater — applies changes to the agent's configuration
based on gap analysis results.

This is the action layer of the learning loop. It takes the
structured analysis from GapAnalyzer and makes the actual
changes to the agent's watch list, keyword patterns, and
search configuration.

Every change is logged permanently to the learning log
so the full change history is auditable by admins.

The updater follows one strict rule:
    It can only ADD to the watch list, never remove.
    Only an Admin can remove sources through the portal.

Usage:
    from learning.watch_list_updater import WatchListUpdater
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    updater = WatchListUpdater(profile)
    changes = updater.apply_changes(analysis)

    for change in changes:
        print(change)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from agent.profile import OrgProfile
from agent.state import AgentState
from learning.learning_log import LearningLog


class WatchListUpdater:
    """
    Applies configuration changes to the agent based on
    gap analysis results.

    Handles three types of changes:
        1. Add new source to watch list
        2. Add keywords to search patterns
        3. Notify admin of changes made

    All changes are logged permanently. The updater never
    removes sources — only admins can do that.
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the watch list updater.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile      = profile
        self.state        = AgentState(profile)
        self.learning_log = LearningLog(profile)

    def apply_changes(self, analysis: dict) -> list[str]:
        """
        Applies all recommended changes from a gap analysis.

        Processes each recommended change in the analysis,
        applies it to the agent's configuration, and returns
        a list of human-readable descriptions of what changed.

        Args:
            analysis: Gap analysis dictionary from GapAnalyzer.

        Returns:
            List of strings describing each change made.
            Empty list if no changes were made.
        """
        changes_made = []

        print(f"[WatchListUpdater] Applying changes for gap type: {analysis.get('gap_type')}")

        # ── Change 1: Add new source to watch list ────────────────────────────
        source_to_add = analysis.get("source_to_add")
        if source_to_add:
            change = self._add_source(
                url        = source_to_add,
                gap_type   = analysis.get("gap_type", "unknown"),
                explanation = analysis.get("explanation", ""),
            )
            if change:
                changes_made.append(change)

        # ── Change 2: Add keywords to search patterns ─────────────────────────
        keywords_to_add = analysis.get("keywords_to_add", [])
        if keywords_to_add:
            change = self._add_keywords(keywords_to_add)
            if change:
                changes_made.append(change)

        # ── Change 3: Handle recommended changes list ─────────────────────────
        for recommendation in analysis.get("recommended_changes", []):
            change = self._process_recommendation(
                recommendation,
                analysis
            )
            if change:
                changes_made.append(change)

        # ── Save state with all changes ───────────────────────────────────────
        if changes_made:
            self.state.save()
            print(f"[WatchListUpdater] {len(changes_made)} change(s) applied and saved")
        else:
            print(f"[WatchListUpdater] No changes were needed")

        return changes_made

    def add_source_from_submission(
        self,
        funder_name:  str,
        source_url:   str,
        funder_url:   Optional[str] = None,
        priority:     str = "medium",
        notes:        str = ""
    ) -> str:
        """
        Directly adds a source from a staff submission without
        requiring a full gap analysis.

        Used when the admin wants to manually add a source
        that was found through a missed grant submission.

        Args:
            funder_name:  Name of the funder.
            source_url:   URL where the grant was found.
            funder_url:   Funder's main website if different.
            priority:     Priority level for monitoring.
            notes:        Reason for adding this source.

        Returns:
            Description of the change made.
        """
        url_to_add = funder_url or source_url

        added = self.state.add_to_watch_list(
            name        = funder_name,
            url         = url_to_add,
            source_type = "foundation_website",
            priority    = priority,
            added_by    = "learning_loop_manual",
            notes       = notes or f"Added from missed grant submission for {funder_name}",
        )

        if added:
            change = (
                f"Added '{funder_name}' to watch list at {url_to_add} "
                f"with {priority} priority."
            )
            self.state.save()

            self.learning_log.log_change(
                change_type  = "source_added",
                description  = change,
                triggered_by = "manual_submission",
                details      = {
                    "funder_name": funder_name,
                    "url":         url_to_add,
                    "priority":    priority,
                }
            )
            return change
        else:
            return f"'{funder_name}' was already in the watch list — no change needed."

    def get_change_history(self, limit: int = 20) -> list[dict]:
        """
        Returns the recent change history from the learning log.

        Used by the admin portal to display what the agent
        has changed about itself over time.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of change log entries, most recent first.
        """
        return self.learning_log.get_recent_changes(limit)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _add_source(
        self,
        url:         str,
        gap_type:    str,
        explanation: str
    ) -> Optional[str]:
        """
        Adds a new source URL to the watch list.

        Args:
            url:         URL to add.
            gap_type:    The type of gap this fixes.
            explanation: Why this source is being added.

        Returns:
            Description of the change or None if already exists.
        """
        if not url or not url.startswith("http"):
            return None

        # Extract a name from the URL if we do not have one
        name = self._extract_name_from_url(url)

        added = self.state.add_to_watch_list(
            name        = name,
            url         = url,
            source_type = "foundation_website",
            priority    = "medium",
            added_by    = "learning_loop",
            notes       = f"Auto-added by learning loop. Gap type: {gap_type}. {explanation[:100]}",
        )

        if added:
            change = (
                f"Added new source to watch list: '{name}' at {url}. "
                f"Gap type resolved: {gap_type}."
            )
            self.learning_log.log_change(
                change_type  = "source_added",
                description  = change,
                triggered_by = "gap_analyzer",
                details      = {
                    "url":      url,
                    "name":     name,
                    "gap_type": gap_type,
                }
            )
            return change

        return None

    def _add_keywords(self, keywords: list[str]) -> Optional[str]:
        """
        Logs new keywords that should be added to search patterns.

        Note: Keywords are logged for now. Full dynamic keyword
        expansion will be implemented in Phase 7 when the keyword
        mapper gets an update mechanism.

        Args:
            keywords: List of new keyword strings to add.

        Returns:
            Description of the change or None if no keywords.
        """
        if not keywords:
            return None

        valid_keywords = [
            kw.strip() for kw in keywords
            if kw and len(kw.strip()) > 3
        ]

        if not valid_keywords:
            return None

        change = (
            f"Identified {len(valid_keywords)} new search keywords "
            f"to improve coverage: {', '.join(valid_keywords[:5])}."
        )

        self.learning_log.log_change(
            change_type  = "keywords_identified",
            description  = change,
            triggered_by = "gap_analyzer",
            details      = {"keywords": valid_keywords}
        )

        return change

    def _process_recommendation(
        self,
        recommendation: str,
        analysis:       dict
    ) -> Optional[str]:
        """
        Processes a single recommended change string from the
        gap analysis and applies it if actionable.

        Args:
            recommendation: A recommended change string.
            analysis:       The full analysis for context.

        Returns:
            Description of what was done or None if not actionable.
        """
        rec_lower = recommendation.lower()

        # If recommendation mentions adding a URL
        if "add" in rec_lower and "http" in recommendation:
            # Extract URL from recommendation
            words = recommendation.split()
            for word in words:
                if word.startswith("http"):
                    return self._add_source(
                        url         = word.strip(".,"),
                        gap_type    = analysis.get("gap_type", "unknown"),
                        explanation = recommendation
                    )

        # Log the recommendation even if not auto-actionable
        self.learning_log.log_change(
            change_type  = "recommendation_logged",
            description  = f"Recommendation noted for admin review: {recommendation[:150]}",
            triggered_by = "gap_analyzer",
            details      = {"recommendation": recommendation}
        )

        return None

    def _extract_name_from_url(self, url: str) -> str:
        """
        Extracts a readable name from a URL.

        e.g. https://www.polkbrosfoundation.org/grants
             → polkbrosfoundation.org

        Args:
            url: URL string.

        Returns:
            Human-readable name derived from the URL.
        """
        try:
            # Remove protocol
            name = url.replace("https://", "").replace("http://", "")
            # Remove www
            name = name.replace("www.", "")
            # Take just the domain
            name = name.split("/")[0]
            return name
        except Exception:
            return url[:50]