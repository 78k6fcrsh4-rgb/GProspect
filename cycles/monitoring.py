"""
cycles/monitoring.py
--------------------
MonitoringCycle — the daily cycle that checks all known sources
for new open grant opportunities.

Runs daily on a schedule set in agent/scheduler.py.
Uses the watch list from AgentState to know what to check.
Filters out previously seen opportunities using AgentState.
Runs the full pipeline: search → filter → score → export.

This cycle is the primary source of day-to-day grant intelligence
for the development team. Results are saved to the outputs folder
and logged to the run history.

Usage:
    from cycles.monitoring import MonitoringCycle
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    cycle   = MonitoringCycle(profile)
    results = cycle.run()
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from agent.profile import OrgProfile
from agent.state import AgentState
from agent.loop import AgentLoop
from agent.keyword_mapper import KeywordMapper
from output.formatter import ResultFormatter
from output.exporter import ResultExporter
from tools.base_tool import GrantOpportunity


class MonitoringCycle:
    """
    Daily monitoring cycle — checks known sources for new opportunities.

    Runs on a daily schedule. Uses the keyword mapper to generate
    search queries from the org profile, runs them through the
    full agent pipeline, filters out previously seen results,
    and exports anything new.

    The cycle is designed to be lightweight — it runs a focused
    set of queries targeting the highest-priority sources rather
    than the full query set. This keeps daily API costs low while
    ensuring nothing important is missed.
    """

    # Number of queries to run per daily cycle
    # Kept lower than a full run to manage daily API costs
    DAILY_QUERY_LIMIT = 15

    # Number of hours to wait between monitoring runs
    # Prevents accidental double-runs
    MIN_HOURS_BETWEEN_RUNS = 20

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the monitoring cycle.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile  = profile
        self.state    = AgentState(profile)
        self.loop     = AgentLoop(profile)
        self.mapper   = KeywordMapper(profile)
        self.formatter = ResultFormatter(profile)
        self.exporter  = ResultExporter(profile)

    def run(
        self,
        force:       bool = False,
        max_queries: int  = None,
    ) -> list[GrantOpportunity]:
        """
        Runs the daily monitoring cycle.

        Steps:
            1. Check if enough time has passed since last run
            2. Build targeted search queries from the keyword mapper
            3. Run the agent pipeline against those queries
            4. Filter out previously seen opportunities
            5. Export new results if any found
            6. Update state with new seen opportunities
            7. Log the run

        Args:
            force:       If True, skip the time check and run anyway.
                        Useful for manual triggers and testing.
            max_queries: Override the default daily query limit.

        Returns:
            List of new GrantOpportunity objects found this cycle.
            Empty list if nothing new found or cycle skipped.
        """
        start_time = time.time()
        run_start  = datetime.now()

        self._print_header("DAILY MONITORING CYCLE")

        # ── Check if we should run ────────────────────────────────────────────
        if not force and not self._should_run():
            print("[MonitoringCycle] Skipping — ran too recently.")
            print(f"[MonitoringCycle] Last run: {self.state.get_last_run()}")
            return []

        # ── Build search queries ──────────────────────────────────────────────
        limit   = max_queries or self.DAILY_QUERY_LIMIT
        queries = self._build_daily_queries(limit)
        print(f"[MonitoringCycle] Running {len(queries)} queries for today's check")

        # ── Run the agent pipeline ────────────────────────────────────────────
        print("[MonitoringCycle] Running agent pipeline...")
        all_results = self.loop.run(
            max_queries    = limit,
            custom_queries = queries,
        )

        if not all_results:
            print("[MonitoringCycle] No results found in this cycle.")
            self._log_run(
                run_start   = run_start,
                queries_run = len(queries),
                raw_count   = 0,
                new_count   = 0,
                elapsed     = time.time() - start_time
            )
            return []

        # ── Filter out previously seen opportunities ───────────────────────────
        new_results = self.state.filter_unseen(all_results)
        print(
            f"[MonitoringCycle] {len(all_results)} found, "
            f"{len(new_results)} are new"
        )

        # ── Export new results ─────────────────────────────────────────────────
        if new_results:
            formatted    = self.formatter.format_all(new_results)
            csv_path     = self.exporter.export_csv(formatted)
            excel_path   = self.exporter.export_excel(formatted)
            summary_path = self.exporter.export_run_summary(
                formatted,
                raw_count      = len(all_results),
                filtered_count = len(new_results)
            )

            print(f"\n[MonitoringCycle] New opportunities exported:")
            print(f"  CSV:   {csv_path}")
            print(f"  Excel: {excel_path}")
            print(f"  Summary: {summary_path}")

            # Mark all new results as seen
            self.state.mark_all_seen(new_results)
        else:
            print("[MonitoringCycle] No new opportunities found today.")

        # ── Log the run and save state ─────────────────────────────────────────
        elapsed = time.time() - start_time
        self._log_run(
            run_start   = run_start,
            queries_run = len(queries),
            raw_count   = len(all_results),
            new_count   = len(new_results),
            elapsed     = elapsed
        )
        self.state.save()

        self._print_footer(len(new_results), elapsed)
        return new_results

    def run_targeted(
        self,
        funder_name: str,
        force:       bool = True
    ) -> list[GrantOpportunity]:
        """
        Runs a targeted monitoring check for a specific funder.

        Used when the admin wants to check a specific foundation
        for new opportunities without running the full cycle.

        Args:
            funder_name: Name of the foundation to check.
            force:       Skip the time check. Default True for
                        targeted runs.

        Returns:
            List of new opportunities from this funder.
        """
        print(f"[MonitoringCycle] Targeted check: {funder_name}")

        queries = [
            f"{funder_name} open grants 2026",
            f"{funder_name} RFP applications open",
            f"{funder_name} grant opportunities deadline",
        ]

        results = self.loop.run(
            max_queries    = 3,
            custom_queries = queries,
        )

        new_results = self.state.filter_unseen(results)

        if new_results:
            formatted = self.formatter.format_all(new_results)
            self.exporter.export_csv(formatted)
            self.state.mark_all_seen(new_results)
            self.state.save()

        return new_results

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_daily_queries(self, limit: int) -> list[str]:
        """
        Builds the daily query set — a focused mix of high-priority
        funder queries and keyword mapper queries.

        The daily set prioritizes:
        1. Direct funder name queries for high-priority known funders
        2. Keyword mapper queries for program areas
        3. Geographic + program combination queries

        Args:
            limit: Maximum number of queries to return.

        Returns:
            List of search query strings.
        """
        queries = []

        # High-priority funder direct queries
        high_priority_sources = self.state.get_high_priority_sources()
        for source in high_priority_sources[:5]:
            queries.append(
                f"{source['name']} open grants 2026 nonprofits"
            )

        # Known funder queries from the org profile
        for funder in self.profile.known_funders[:5]:
            queries.append(
                f"{funder.name} open grants applications 2026"
            )

        # Keyword mapper queries — fill remaining slots
        mapper_queries = self.mapper.build_search_queries()
        remaining      = limit - len(queries)
        queries.extend(mapper_queries[:remaining])

        return queries[:limit]

    def _should_run(self) -> bool:
        """
        Checks whether enough time has passed since the last run
        to justify running the monitoring cycle again.

        Returns:
            True if the cycle should run, False if too recent.
        """
        last_run = self.state.get_last_run()
        if not last_run:
            return True

        try:
            last_run_time = datetime.fromisoformat(last_run["timestamp"])
            hours_elapsed = (
                datetime.now() - last_run_time
            ).total_seconds() / 3600

            return hours_elapsed >= self.MIN_HOURS_BETWEEN_RUNS
        except (KeyError, ValueError):
            return True

    def _log_run(
        self,
        run_start:   datetime,
        queries_run: int,
        raw_count:   int,
        new_count:   int,
        elapsed:     float
    ) -> None:
        """
        Logs the completed monitoring run to state.

        Args:
            run_start:   When the run started.
            queries_run: Number of queries executed.
            raw_count:   Total raw results before dedup filter.
            new_count:   New opportunities not seen before.
            elapsed:     Total run time in seconds.
        """
        self.state.log_run(
            queries_run   = queries_run,
            raw_results   = raw_count,
            filtered      = new_count,
            final_results = new_count,
            cycle_type    = "monitoring",
        )

    def _print_header(self, title: str) -> None:
        """Prints a formatted cycle header."""
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"  Organization: {self.profile.org_name}")
        print(f"  Started: {datetime.now().strftime('%B %d, %Y at %H:%M:%S')}")
        print(f"{'='*60}")

    def _print_footer(self, new_count: int, elapsed: float) -> None:
        """Prints a formatted cycle completion message."""
        print(f"\n{'='*60}")
        print(f"  MONITORING CYCLE COMPLETE")
        print(f"  New opportunities found: {new_count}")
        print(f"  Run time: {round(elapsed, 1)} seconds")
        print(f"{'='*60}\n")