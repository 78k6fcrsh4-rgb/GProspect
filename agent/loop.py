"""
agent/loop.py
-------------
AgentLoop — the main orchestration pipeline for the grant
prospecting agent.

This is the central coordinator that connects all components:
    Profile → KeywordMapper → ToolRegistry → EligibilityFilter
    → GrantScorer → Results

One call to AgentLoop.run() executes the complete prospecting
pipeline and returns a ranked list of scored opportunities.

The loop is designed to be called by:
- run_agent.py (CLI entry point)
- cycles/monitoring.py (daily monitoring cycle)
- cycles/discovery.py (weekly discovery cycle)
- The web portal backend (on-demand runs)

Usage:
    from agent.loop import AgentLoop
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    loop    = AgentLoop(profile)
    results = loop.run(max_queries=5)

    for opp in results:
        print(opp.display_summary())
        print(f'Score: {opp.score_final}')
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from agent.profile import OrgProfile
from agent.keyword_mapper import KeywordMapper
from agent.prompt_builder import PromptBuilder
from tools import ToolRegistry
from tools.base_tool import GrantOpportunity
from scoring.eligibility import EligibilityFilter
from scoring.scorer import GrantScorer


class AgentLoop:
    """
    The main orchestration pipeline for the grant prospecting agent.

    Connects all components in the correct sequence and manages
    the full prospecting run from profile to ranked results.

    The loop handles:
    - Query generation from the keyword mapper
    - Multi-tool search execution via the tool registry
    - Eligibility filtering
    - AI scoring and ranking
    - Run logging and timing
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the agent loop with an org profile.

        Initializes all components with the profile so they are
        ready to run immediately when run() is called.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile        = profile
        self.keyword_mapper = KeywordMapper(profile)
        self.prompt_builder = PromptBuilder(profile)
        self.tool_registry  = ToolRegistry(profile)
        self.eligibility    = EligibilityFilter(profile)
        self.scorer         = GrantScorer(profile)
        self.run_log        = []

    def run(
        self,
        max_queries:    int           = 10,
        custom_queries: Optional[list[str]] = None,
        skip_scoring:   bool          = False,
    ) -> list[GrantOpportunity]:
        """
        Executes the complete grant prospecting pipeline.

        Steps:
            1. Generate search queries from the keyword mapper
               (or use custom_queries if provided)
            2. Run all search tools against each query
            3. Apply eligibility filter to raw results
            4. Score and rank everything that passed
            5. Return ranked results

        Args:
            max_queries:    Maximum number of queries to run.
                           Higher = more results but more API cost.
                           Default 10 is good for testing.
                           Use 20-30 for production runs.
            custom_queries: Optional list of specific queries to run
                           instead of the auto-generated ones.
                           Useful for targeted searches.
            skip_scoring:  If True, skip the AI scoring step and
                           return unscored filtered results.
                           Useful for testing the search layer.

        Returns:
            List of GrantOpportunity objects, scored and ranked
            by final score descending. Empty list if nothing found.
        """
        start_time = time.time()
        run_start  = datetime.now()

        self._log(f"Starting prospecting run for: {self.profile.org_name}")
        self._log(f"Run started at: {run_start.strftime('%Y-%m-%d %H:%M:%S')}")
        self._log("=" * 60)

        # ── Step 1: Generate search queries ──────────────────────────────────
        if custom_queries:
            queries = custom_queries
            self._log(f"Using {len(queries)} custom queries")
        else:
            all_queries = self.keyword_mapper.build_search_queries()
            queries     = all_queries[:max_queries]
            self._log(f"Generated {len(all_queries)} queries, running {len(queries)}")

        # ── Step 2: Run search tools ──────────────────────────────────────────
        self._log("\nRunning search tools...")
        self.tool_registry.print_status()

        raw_results = self.tool_registry.run_all_queries(
            queries    = queries,
            max_queries = max_queries
        )

        self._log(f"Raw results from all tools: {len(raw_results)}")

        if not raw_results:
            self._log("No results found. Run complete.")
            return []

        # ── Step 3: Apply eligibility filter ─────────────────────────────────
        self._log("\nApplying eligibility filter...")
        self._log(self.eligibility.get_exclusion_summary())

        filtered = self.eligibility.filter(raw_results)
        self._log(f"After filtering: {len(filtered)} opportunities")

        if not filtered:
            self._log("No opportunities passed eligibility filter.")
            self._log("This may mean all results were federal grants (excluded by profile settings)")
            self._log("or all deadlines were outside the configured range.")
            return []

        # ── Step 4: Score and rank ────────────────────────────────────────────
        if skip_scoring:
            self._log("\nSkipping scoring (skip_scoring=True)")
            results = filtered
        else:
            self._log(f"\nScoring {len(filtered)} opportunities with AI...")
            results = self.scorer.score_all(filtered)
            self._log(f"After scoring: {len(results)} opportunities above threshold")

        # ── Step 5: Log results summary ───────────────────────────────────────
        elapsed = round(time.time() - start_time, 1)
        self._log("\n" + "=" * 60)
        self._log(f"RUN COMPLETE — {elapsed} seconds")
        self._log(f"Final results: {len(results)} ranked opportunities")
        self._log("=" * 60)

        if results:
            self._log("\nTop results:")
            for i, opp in enumerate(results[:5], 1):
                score_str = f"Score: {opp.score_final}" if opp.score_final else "Unscored"
                self._log(f"  {i}. [{score_str}] {opp.funder_name} — {opp.program_name[:50]}")

        return results

    def run_targeted(
        self,
        program_area: str,
        max_queries:  int = 5
    ) -> list[GrantOpportunity]:
        """
        Runs a targeted search for a specific program area only.

        Useful when the development team wants to find grants for
        one specific program rather than running the full pipeline.

        Args:
            program_area: Program area string e.g. 'workforce_development'
            max_queries:  Maximum queries to run for this area.

        Returns:
            Ranked list of GrantOpportunity objects for that program.
        """
        self._log(f"Running targeted search for program: {program_area}")

        queries = self.keyword_mapper.queries_for_program(program_area)
        return self.run(
            max_queries    = max_queries,
            custom_queries = queries[:max_queries]
        )

    def get_run_log(self) -> list[str]:
        """
        Returns the log entries from the most recent run.

        Used by the portal to display run history and by the
        learning loop to analyze what the agent searched for.

        Returns:
            List of log entry strings.
        """
        return self.run_log.copy()

    def print_run_log(self) -> None:
        """Prints all log entries to the terminal."""
        for entry in self.run_log:
            print(entry)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        """
        Adds a message to the run log and prints it.

        Args:
            message: Log message string.
        """
        self.run_log.append(message)
        print(message)