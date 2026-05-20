"""
agent/state.py
--------------
AgentState — persistent session memory for the grant prospecting agent.

Tracks:
    1. Seen opportunities — prevents the same grant appearing in
       multiple runs. Once an opportunity is seen it is not
       surfaced again unless it has been updated by the funder.

    2. Watch list — the growing list of sources the monitoring
       cycle checks every day. Starts with seed sources and
       grows as the discovery cycle finds new ones.

    3. Run history — a log of every prospecting run including
       when it ran, how many results were found, and what
       queries were used.

State is persisted to a JSON file so it survives between runs.
The file is stored in the outputs directory alongside results.

Usage:
    from agent.state import AgentState
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    state   = AgentState(profile)

    # Check if an opportunity has been seen before
    if not state.is_seen(opportunity):
        state.mark_seen(opportunity)
        # process the opportunity

    # Add a new source to the watch list
    state.add_to_watch_list("https://www.newfoundation.org/grants")

    # Save state to disk
    state.save()
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from agent.profile import OrgProfile
from tools.base_tool import GrantOpportunity


# ─────────────────────────────────────────────────────────────────────────────
# Default seed watch list
# These are the sources the monitoring cycle checks from day one.
# The discovery cycle and learning loop add more over time.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_WATCH_LIST = [
    # Chicago-based foundations — highest priority for Deborah's Place
    {
        "name":     "Polk Bros. Foundation",
        "url":      "https://www.polkbrosfoundation.org/grants",
        "type":     "foundation_website",
        "priority": "high",
        "added_by": "seed",
    },
    {
        "name":     "MacArthur Foundation",
        "url":      "https://www.macfound.org/grants",
        "type":     "foundation_website",
        "priority": "high",
        "added_by": "seed",
    },
    {
        "name":     "Chicago Community Trust",
        "url":      "https://www.cct.org/grants",
        "type":     "foundation_website",
        "priority": "high",
        "added_by": "seed",
    },
    {
        "name":     "Chicago Foundation for Women",
        "url":      "https://www.cfw.org/grants",
        "type":     "foundation_website",
        "priority": "high",
        "added_by": "seed",
    },
    {
        "name":     "Joyce Foundation",
        "url":      "https://www.joycefdn.org/grants",
        "type":     "foundation_website",
        "priority": "high",
        "added_by": "seed",
    },
    # National foundations
    {
        "name":     "Robert Wood Johnson Foundation",
        "url":      "https://www.rwjf.org/en/grants",
        "type":     "foundation_website",
        "priority": "medium",
        "added_by": "seed",
    },
    {
        "name":     "W.K. Kellogg Foundation",
        "url":      "https://www.wkkf.org/grants",
        "type":     "foundation_website",
        "priority": "medium",
        "added_by": "seed",
    },
    {
        "name":     "Ms. Foundation for Women",
        "url":      "https://forwomen.org/grants",
        "type":     "foundation_website",
        "priority": "medium",
        "added_by": "seed",
    },
    # Grant aggregators
    {
        "name":     "Philanthropy News Digest",
        "url":      "https://philanthropynewsdigest.org/rfps",
        "type":     "aggregator",
        "priority": "medium",
        "added_by": "seed",
    },
    {
        "name":     "Grants.gov",
        "url":      "https://www.grants.gov",
        "type":     "federal_database",
        "priority": "low",
        "added_by": "seed",
    },
]


class AgentState:
    """
    Persistent session memory for the grant prospecting agent.

    Manages the seen opportunities set, the source watch list,
    and the run history log. All state is persisted to a JSON
    file so it survives between runs and across sessions.
    """

    def __init__(
        self,
        profile:    OrgProfile,
        state_dir:  str = "outputs"
    ) -> None:
        """
        Initialize the agent state for the given org profile.

        Loads existing state from disk if available, otherwise
        starts fresh with the default watch list.

        Args:
            profile:   Loaded and validated OrgProfile.
            state_dir: Directory where state files are stored.
        """
        self.profile   = profile
        self.state_dir = Path(state_dir)

        # Build the state file path — one file per org
        org_slug        = (
            profile.org_short_name
            .lower()
            .replace(" ", "_")
            .replace("'", "")
        )
        self.state_file = self.state_dir / org_slug / "agent_state.json"

        # Initialize state containers
        self.seen_fingerprints: set[str]  = set()
        self.watch_list:        list[dict] = []
        self.run_history:       list[dict] = []
        self.known_opportunity_ids: set[str] = set()

        # Load from disk or initialize fresh
        self._load()

    # ── Seen opportunity tracking ─────────────────────────────────────────────

    def is_seen(self, opp: GrantOpportunity) -> bool:
        """
        Checks whether this opportunity has been seen in a previous run.

        Uses a fingerprint of funder name + program name for matching.
        Case-insensitive and whitespace-normalized.

        Args:
            opp: GrantOpportunity to check.

        Returns:
            True if this opportunity has been seen before.
        """
        fp = self._fingerprint(opp)
        return fp in self.seen_fingerprints

    def mark_seen(self, opp: GrantOpportunity) -> None:
        """
        Marks an opportunity as seen so it is not surfaced again.

        Args:
            opp: GrantOpportunity to mark as seen.
        """
        fp = self._fingerprint(opp)
        self.seen_fingerprints.add(fp)
        self.known_opportunity_ids.add(opp.opportunity_id)

    def filter_unseen(
        self,
        opportunities: list[GrantOpportunity]
    ) -> list[GrantOpportunity]:
        """
        Filters a list of opportunities to only those not seen before.

        Used by the monitoring cycle to skip opportunities that have
        already been surfaced in previous runs.

        Args:
            opportunities: List of GrantOpportunity objects.

        Returns:
            List containing only opportunities not seen before.
        """
        unseen = [opp for opp in opportunities if not self.is_seen(opp)]
        seen_count = len(opportunities) - len(unseen)

        if seen_count > 0:
            print(f"[AgentState] Filtered out {seen_count} previously seen opportunities")

        return unseen

    def mark_all_seen(self, opportunities: list[GrantOpportunity]) -> None:
        """
        Marks all opportunities in a list as seen.

        Called after a successful run to update the seen set.

        Args:
            opportunities: List of opportunities to mark as seen.
        """
        for opp in opportunities:
            self.mark_seen(opp)

    # ── Watch list management ─────────────────────────────────────────────────

    def get_watch_list(self) -> list[dict]:
        """
        Returns the current watch list of sources to monitor.

        Returns:
            List of source dictionaries with name, url, type,
            priority, and added_by fields.
        """
        return self.watch_list.copy()

    def add_to_watch_list(
        self,
        name:     str,
        url:      str,
        source_type: str  = "foundation_website",
        priority: str     = "medium",
        added_by: str     = "discovery_cycle",
        notes:    Optional[str] = None
    ) -> bool:
        """
        Adds a new source to the watch list.

        Checks for duplicates before adding — the same URL
        will not be added twice.

        Args:
            name:        Human-readable name of the source.
            url:         URL to monitor.
            source_type: Type of source e.g. foundation_website,
                        aggregator, news_feed.
            priority:    Priority level: high, medium, or low.
            added_by:    What added this source: seed, discovery_cycle,
                        learning_loop, or admin.
            notes:       Optional notes about why this was added.

        Returns:
            True if added, False if already in watch list.
        """
        # Check for duplicate URL
        existing_urls = {s["url"].lower() for s in self.watch_list}
        if url.lower() in existing_urls:
            return False

        entry = {
            "name":       name,
            "url":        url,
            "type":       source_type,
            "priority":   priority,
            "added_by":   added_by,
            "added_date": date.today().isoformat(),
            "notes":      notes or "",
        }

        self.watch_list.append(entry)
        print(f"[AgentState] Added to watch list: {name} ({url})")
        return True

    def remove_from_watch_list(self, url: str) -> bool:
        """
        Removes a source from the watch list by URL.

        Only Admin can remove sources — this method is called
        by the portal's admin router, not by the agent itself.

        Args:
            url: URL of the source to remove.

        Returns:
            True if removed, False if not found.
        """
        original_count = len(self.watch_list)
        self.watch_list = [
            s for s in self.watch_list
            if s["url"].lower() != url.lower()
        ]
        removed = len(self.watch_list) < original_count
        if removed:
            print(f"[AgentState] Removed from watch list: {url}")
        return removed

    def get_high_priority_sources(self) -> list[dict]:
        """
        Returns only high-priority sources from the watch list.

        Used by the monitoring cycle when running a quick daily
        check focused on the most important sources.

        Returns:
            List of high-priority source dictionaries.
        """
        return [s for s in self.watch_list if s.get("priority") == "high"]

    # ── Run history ───────────────────────────────────────────────────────────

    def log_run(
        self,
        queries_run:    int,
        raw_results:    int,
        filtered:       int,
        final_results:  int,
        cycle_type:     str = "manual",
        output_path:    Optional[str] = None
    ) -> None:
        """
        Logs a completed prospecting run to the run history.

        Args:
            queries_run:   Number of search queries executed.
            raw_results:   Total raw results before filtering.
            filtered:      Results that passed eligibility filter.
            final_results: Final ranked results after scoring.
            cycle_type:    Type of run: manual, monitoring,
                          discovery, or relationship_map.
            output_path:   Path to the output files if exported.
        """
        entry = {
            "timestamp":     datetime.now().isoformat(),
            "cycle_type":    cycle_type,
            "queries_run":   queries_run,
            "raw_results":   raw_results,
            "filtered":      filtered,
            "final_results": final_results,
            "output_path":   output_path or "",
        }
        self.run_history.append(entry)
        print(
            f"[AgentState] Run logged: {cycle_type} — "
            f"{final_results} results from {queries_run} queries"
        )

    def get_run_history(self, limit: int = 10) -> list[dict]:
        """
        Returns the most recent run history entries.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of run history dictionaries, most recent first.
        """
        return list(reversed(self.run_history[-limit:]))

    def get_last_run(self) -> Optional[dict]:
        """
        Returns the most recent run entry.

        Returns:
            Most recent run dictionary or None if no runs yet.
        """
        if not self.run_history:
            return None
        return self.run_history[-1]

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self) -> None:
        """
        Saves the current state to disk as a JSON file.

        Creates the state directory if it does not exist.
        Called after every run and after any watch list changes.
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        state_data = {
            "org_name":              self.profile.org_name,
            "last_saved":            datetime.now().isoformat(),
            "seen_fingerprints":     list(self.seen_fingerprints),
            "known_opportunity_ids": list(self.known_opportunity_ids),
            "watch_list":            self.watch_list,
            "run_history":           self.run_history,
        }

        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)

        print(f"[AgentState] State saved: {self.state_file}")

    def reset_seen(self) -> None:
        """
        Clears the seen opportunities set.

        Use this when you want the agent to re-surface all
        opportunities as if it had never run before.
        Useful after a long gap between runs.
        """
        count = len(self.seen_fingerprints)
        self.seen_fingerprints.clear()
        self.known_opportunity_ids.clear()
        print(f"[AgentState] Cleared {count} seen opportunities")

    def get_stats(self) -> dict:
        """
        Returns current state statistics.

        Returns:
            Dictionary with counts of seen opportunities,
            watch list size, and run history length.
        """
        return {
            "seen_opportunities": len(self.seen_fingerprints),
            "watch_list_size":    len(self.watch_list),
            "total_runs":         len(self.run_history),
            "last_run":           self.run_history[-1]["timestamp"] if self.run_history else "Never",
        }

    def print_status(self) -> None:
        """
        Prints a human-readable status summary.
        Used in the agent startup sequence and portal display.
        """
        stats = self.get_stats()
        print(f"\n{'='*50}")
        print(f"Agent State — {self.profile.org_short_name}")
        print(f"{'='*50}")
        print(f"  Seen opportunities: {stats['seen_opportunities']}")
        print(f"  Watch list sources: {stats['watch_list_size']}")
        print(f"  Total runs logged:  {stats['total_runs']}")
        print(f"  Last run:           {stats['last_run']}")
        print(f"  State file:         {self.state_file}")
        print(f"{'='*50}\n")

    # ── Private helpers ───────────────────────────────────────────────────────

    def _fingerprint(self, opp: GrantOpportunity) -> str:
        """
        Generates a unique fingerprint for an opportunity.

        Based on normalized funder name and program name.
        Used to detect duplicate opportunities across runs.

        Args:
            opp: GrantOpportunity to fingerprint.

        Returns:
            MD5 hash string uniquely identifying this opportunity.
        """
        raw = (
            opp.funder_name.lower().strip() +
            "|" +
            opp.program_name.lower().strip()
        )
        return hashlib.md5(raw.encode()).hexdigest()

    def _load(self) -> None:
        """
        Loads state from disk if a state file exists.

        If no state file exists, initializes fresh state with
        the default watch list as the starting point.
        """
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                self.seen_fingerprints     = set(data.get("seen_fingerprints", []))
                self.known_opportunity_ids = set(data.get("known_opportunity_ids", []))
                self.watch_list            = data.get("watch_list", DEFAULT_WATCH_LIST)
                self.run_history           = data.get("run_history", [])

                print(
                    f"[AgentState] Loaded state for {self.profile.org_short_name} — "
                    f"{len(self.seen_fingerprints)} seen, "
                    f"{len(self.watch_list)} sources"
                )
                return

            except (json.JSONDecodeError, KeyError) as e:
                print(f"[AgentState] Could not load state file: {e} — starting fresh")

        # No state file — initialize fresh
        print(f"[AgentState] No existing state found — initializing fresh state")
        self.watch_list = DEFAULT_WATCH_LIST.copy()
        self.save()