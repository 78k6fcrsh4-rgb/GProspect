"""
cycles/discovery.py
-------------------
DiscoveryCycle — the weekly cycle that finds NEW funding sources
not yet on the agent's watch list.

Runs weekly on a schedule set in agent/scheduler.py.
Expands the watch list by finding foundations whose giving
patterns align with the org profile.

Sources used:
    - IRS Form 990 data via ProPublica (free, no key needed)
    - Web search for philanthropy news and new funding announcements
    - Co-funder relationship mining from known funders

New sources discovered are evaluated against the org profile
and added to the watch list if they meet the relevance threshold.
The monitoring cycle then picks them up automatically.

Usage:
    from cycles.discovery import DiscoveryCycle
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    cycle   = DiscoveryCycle(profile)
    new_sources = cycle.run()
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from agent.profile import OrgProfile
from agent.state import AgentState
from agent.keyword_mapper import KeywordMapper
from tools.web_search import WebSearchTool
from tools.form_990 import Form990Tool


class DiscoveryCycle:
    """
    Weekly discovery cycle — finds new funding sources for the watch list.

    Searches broadly for foundations and funding organizations whose
    giving patterns align with the org profile. Each discovered source
    is evaluated for relevance before being added to the watch list.

    The cycle is designed to expand the agent's intelligence over time.
    Week 1 may find 2-3 new sources. Month 3 may have found 50+.
    """

    # Number of discovery queries to run per weekly cycle
    WEEKLY_QUERY_LIMIT = 10

    # Minimum days between discovery runs
    MIN_DAYS_BETWEEN_RUNS = 6

    # Keywords that indicate a source is a funder or grant opportunity
    FUNDER_INDICATORS = [
        "foundation", "grant", "funding", "rfp", "apply",
        "nonprofit", "philanthropy", "award", "initiative",
        "fund", "grantmaking", "giving"
    ]

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the discovery cycle.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile    = profile
        self.state      = AgentState(profile)
        self.mapper     = KeywordMapper(profile)
        self.web_search = WebSearchTool(profile)
        self.form_990   = Form990Tool(profile)

    def run(
        self,
        force:       bool = False,
        max_queries: int  = None,
    ) -> list[dict]:
        """
        Runs the weekly discovery cycle.

        Steps:
            1. Check if enough time has passed since last run
            2. Build discovery queries from the keyword mapper
            3. Search for new foundations via web search
            4. Search for aligned foundations via 990 data
            5. Evaluate each discovered source for relevance
            6. Add qualifying sources to the watch list
            7. Log the run and save state

        Args:
            force:       Skip the time check and run anyway.
            max_queries: Override the default weekly query limit.

        Returns:
            List of new source dictionaries added to the watch list.
        """
        start_time = time.time()

        self._print_header("WEEKLY DISCOVERY CYCLE")

        # ── Check if we should run ────────────────────────────────────────────
        if not force and not self._should_run():
            print("[DiscoveryCycle] Skipping — ran too recently.")
            return []

        limit           = max_queries or self.WEEKLY_QUERY_LIMIT
        new_sources     = []
        existing_urls   = {
            s["url"].lower()
            for s in self.state.get_watch_list()
        }

        # ── Phase 1: Web search for new funders ───────────────────────────────
        print("\n[DiscoveryCycle] Phase 1: Searching web for new funders...")
        web_sources = self._discover_via_web_search(limit // 2, existing_urls)
        new_sources.extend(web_sources)
        print(f"[DiscoveryCycle] Web search found {len(web_sources)} new sources")

        # ── Phase 2: 990 data mining ──────────────────────────────────────────
        print("\n[DiscoveryCycle] Phase 2: Mining IRS 990 data...")
        irs_sources = self._discover_via_990(limit // 2, existing_urls)
        new_sources.extend(irs_sources)
        print(f"[DiscoveryCycle] 990 mining found {len(irs_sources)} new sources")

        # ── Phase 3: Add to watch list ────────────────────────────────────────
        added = []
        for source in new_sources:
            success = self.state.add_to_watch_list(
                name        = source["name"],
                url         = source["url"],
                source_type = source.get("type", "foundation_website"),
                priority    = source.get("priority", "medium"),
                added_by    = "discovery_cycle",
                notes       = source.get("notes", ""),
            )
            if success:
                added.append(source)

        # ── Log and save ──────────────────────────────────────────────────────
        elapsed = time.time() - start_time
        self.state.log_run(
            queries_run   = limit,
            raw_results   = len(new_sources),
            filtered      = len(added),
            final_results = len(added),
            cycle_type    = "discovery",
        )
        self.state.save()

        self._print_footer(len(added), elapsed)
        return added

    def evaluate_source(self, name: str, url: str) -> dict:
        """
        Evaluates a potential new source for relevance to the org profile.

        Used by the learning loop when a staff member submits a missed
        grant — the discovery cycle evaluates the source it came from
        and decides whether to add it to the watch list.

        Args:
            name: Name of the source to evaluate.
            url:  URL of the source.

        Returns:
            Dictionary with evaluation results including relevance
            score and recommendation.
        """
        org = self.profile

        # Build evaluation criteria from profile
        program_keywords = [
            p.value.replace("_", " ")
            for p in org.program_areas
        ]
        population_keywords = [
            p.value.replace("_", " ")
            for p in org.populations_served
        ]

        name_lower = name.lower()
        url_lower  = url.lower()
        combined   = name_lower + " " + url_lower

        # Score relevance based on keyword presence
        relevance_score = 0

        # Check for funder indicators
        for indicator in self.FUNDER_INDICATORS:
            if indicator in combined:
                relevance_score += 1

        # Check for program area alignment
        for keyword in program_keywords:
            if any(word in combined for word in keyword.split()):
                relevance_score += 2

        # Check for population alignment
        for keyword in population_keywords[:3]:
            if keyword.split()[0] in combined:
                relevance_score += 2

        # Check for geographic alignment
        geo_terms = [
            self.profile.geography.city.lower(),
            self.profile.geography.state.lower(),
        ]
        for term in geo_terms:
            if term in combined:
                relevance_score += 3

        # Determine recommendation
        if relevance_score >= 8:
            recommendation = "add_high_priority"
            priority       = "high"
        elif relevance_score >= 4:
            recommendation = "add_medium_priority"
            priority       = "medium"
        elif relevance_score >= 2:
            recommendation = "add_low_priority"
            priority       = "low"
        else:
            recommendation = "skip"
            priority       = None

        return {
            "name":           name,
            "url":            url,
            "relevance_score": relevance_score,
            "recommendation": recommendation,
            "priority":       priority,
            "should_add":     recommendation != "skip",
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _discover_via_web_search(
        self,
        max_queries:   int,
        existing_urls: set
    ) -> list[dict]:
        """
        Discovers new funding sources through web search.

        Uses funder-discovery queries from the keyword mapper to
        find foundations and funding organizations not yet on
        the watch list.

        Args:
            max_queries:   Maximum queries to run.
            existing_urls: Set of URLs already in the watch list.

        Returns:
            List of new source dictionaries.
        """
        queries     = self.mapper.get_funder_search_queries()[:max_queries]
        new_sources = []

        for query in queries:
            try:
                results = self.web_search.run(query)
                time.sleep(2)

                for result in results:
                    if not result.funder_website:
                        continue

                    url = result.funder_website.lower()
                    if url in existing_urls:
                        continue

                    evaluation = self.evaluate_source(
                        result.funder_name,
                        result.funder_website
                    )

                    if evaluation["should_add"]:
                        new_sources.append({
                            "name":     result.funder_name,
                            "url":      result.funder_website,
                            "type":     "foundation_website",
                            "priority": evaluation["priority"],
                            "notes":    f"Found via discovery cycle query: {query[:60]}",
                        })
                        existing_urls.add(url)

            except Exception as e:
                print(f"[DiscoveryCycle] Web search error for '{query}': {e}")
                continue

        return new_sources

    def _discover_via_990(
        self,
        max_queries:   int,
        existing_urls: set
    ) -> list[dict]:
        """
        Discovers new funding sources through IRS 990 data mining.

        Searches for foundations whose giving patterns in their
        990 filings align with the org profile.

        Args:
            max_queries:   Maximum queries to run.
            existing_urls: Set of URLs already in the watch list.

        Returns:
            List of new source dictionaries.
        """
        # Build 990-specific search queries
        queries = []
        city    = self.profile.geography.city

        for program in self.profile.program_areas[:3]:
            queries.append(
                f"{program.value.replace('_', ' ')} foundation {city}"
            )

        for population in self.profile.populations_served[:2]:
            queries.append(
                f"{population.value.replace('_', ' ')} services foundation grants"
            )

        new_sources = []

        for query in queries[:max_queries]:
            try:
                results = self.form_990.run(query)
                time.sleep(1)

                for result in results:
                    if not result.source_url:
                        continue

                    url = result.source_url.lower()
                    if url in existing_urls:
                        continue

                    evaluation = self.evaluate_source(
                        result.funder_name,
                        result.source_url
                    )

                    if evaluation["should_add"]:
                        new_sources.append({
                            "name":     result.funder_name,
                            "url":      result.source_url,
                            "type":     "foundation_990",
                            "priority": evaluation["priority"],
                            "notes":    (
                                f"Discovered via IRS 990 data. "
                                f"Relevance score: {evaluation['relevance_score']}. "
                                f"Query: {query[:60]}"
                            ),
                        })
                        existing_urls.add(url)

            except Exception as e:
                print(f"[DiscoveryCycle] 990 search error for '{query}': {e}")
                continue

        return new_sources

    def _should_run(self) -> bool:
        """
        Checks whether enough time has passed since the last
        discovery run to justify running again.

        Returns:
            True if the cycle should run, False if too recent.
        """
        last_run = self.state.get_last_run()
        if not last_run:
            return True

        try:
            last_time    = datetime.fromisoformat(last_run["timestamp"])
            days_elapsed = (datetime.now() - last_time).days
            return days_elapsed >= self.MIN_DAYS_BETWEEN_RUNS
        except (KeyError, ValueError):
            return True

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
        print(f"  DISCOVERY CYCLE COMPLETE")
        print(f"  New sources added to watch list: {new_count}")
        print(f"  Run time: {round(elapsed, 1)} seconds")
        print(f"{'='*60}\n")