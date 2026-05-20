"""
cycles/relationship_map.py
--------------------------
RelationshipMapCycle — the monthly cycle that builds and maintains
a living intelligence map of the funding landscape.

Runs monthly on a schedule set in agent/scheduler.py.

What this cycle produces:
    1. Warm path map — foundations aligned with the org that
       have no open RFP but whose giving history suggests
       they should be cultivated now
    2. Co-funder network — foundations that give alongside
       known funders, revealing warm introduction paths
    3. Strategic timing signals — funders with predictable
       cycles where relationship building should begin weeks
       before the RFP drops
    4. Prior funder activity — known funders with recent
       giving activity suggesting an upcoming cycle

Results are saved to the outputs folder as a relationship
intelligence report alongside the standard prospect list.

Usage:
    from cycles.relationship_map import RelationshipMapCycle
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    cycle   = RelationshipMapCycle(profile)
    report  = cycle.run()
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from agent.profile import OrgProfile, KnownFunder
from agent.state import AgentState
from agent.keyword_mapper import KeywordMapper
from tools.form_990 import Form990Tool
from tools.web_search import WebSearchTool


class RelationshipMapCycle:
    """
    Monthly relationship mapping cycle.

    Builds a living intelligence map of the funding landscape
    by analyzing giving patterns, co-funder relationships,
    and strategic timing signals.

    Unlike the monitoring and discovery cycles which look for
    open opportunities, this cycle looks for strategic intelligence
    that helps the development team build the right relationships
    before grant cycles open.
    """

    # Minimum days between relationship map runs
    MIN_DAYS_BETWEEN_RUNS = 28

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the relationship mapping cycle.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile    = profile
        self.state      = AgentState(profile)
        self.mapper     = KeywordMapper(profile)
        self.form_990   = Form990Tool(profile)
        self.web_search = WebSearchTool(profile)

    def run(
        self,
        force: bool = False
    ) -> dict:
        """
        Runs the monthly relationship mapping cycle.

        Steps:
            1. Check timing
            2. Analyze known funder relationships
            3. Find co-funders via 990 data
            4. Identify warm path prospects
            5. Surface strategic timing signals
            6. Save relationship intelligence report
            7. Log run and save state

        Args:
            force: Skip timing check and run anyway.

        Returns:
            Dictionary containing the full relationship
            intelligence report.
        """
        start_time = time.time()

        self._print_header("MONTHLY RELATIONSHIP MAPPING CYCLE")

        # ── Check timing ──────────────────────────────────────────────────────
        if not force and not self._should_run():
            print("[RelationshipMap] Skipping — ran too recently.")
            return {}

        report = {
            "org_name":        self.profile.org_name,
            "run_date":        date.today().isoformat(),
            "warm_paths":      [],
            "co_funders":      [],
            "timing_signals":  [],
            "prior_funder_activity": [],
            "summary":         "",
        }

        # ── Phase 1: Analyze known funders ────────────────────────────────────
        print("\n[RelationshipMap] Phase 1: Analyzing known funder relationships...")
        prior_activity = self._analyze_known_funders()
        report["prior_funder_activity"] = prior_activity
        print(f"[RelationshipMap] Analyzed {len(self.profile.known_funders)} known funders")

        # ── Phase 2: Find co-funders ──────────────────────────────────────────
        print("\n[RelationshipMap] Phase 2: Finding co-funder relationships...")
        co_funders = self._find_co_funders()
        report["co_funders"] = co_funders
        print(f"[RelationshipMap] Found {len(co_funders)} potential co-funder relationships")

        # ── Phase 3: Identify warm paths ──────────────────────────────────────
        print("\n[RelationshipMap] Phase 3: Identifying warm path prospects...")
        warm_paths = self._identify_warm_paths()
        report["warm_paths"] = warm_paths
        print(f"[RelationshipMap] Identified {len(warm_paths)} warm path prospects")

        # ── Phase 4: Surface timing signals ───────────────────────────────────
        print("\n[RelationshipMap] Phase 4: Surfacing strategic timing signals...")
        timing_signals = self._find_timing_signals()
        report["timing_signals"] = timing_signals
        print(f"[RelationshipMap] Found {len(timing_signals)} timing signals")

        # ── Phase 5: Build summary ────────────────────────────────────────────
        report["summary"] = self._build_summary(report)

        # ── Phase 6: Save report ──────────────────────────────────────────────
        report_path = self._save_report(report)
        print(f"\n[RelationshipMap] Report saved: {report_path}")

        # ── Log run ───────────────────────────────────────────────────────────
        elapsed = time.time() - start_time
        self.state.log_run(
            queries_run   = 0,
            raw_results   = len(warm_paths) + len(co_funders),
            filtered      = len(warm_paths),
            final_results = len(warm_paths),
            cycle_type    = "relationship_map",
        )
        self.state.save()

        self._print_footer(report, elapsed)
        return report

    def get_warm_paths(self) -> list[dict]:
        """
        Returns the current warm path prospects from the
        most recent relationship map report.

        Used by the portal to display strategic cultivation
        targets alongside the regular prospect list.

        Returns:
            List of warm path prospect dictionaries.
        """
        report_dir = Path("outputs") / self._org_slug()
        report_files = sorted(report_dir.glob("relationship_map_*.json"))

        if not report_files:
            return []

        try:
            with open(report_files[-1], "r") as f:
                report = json.load(f)
            return report.get("warm_paths", [])
        except Exception:
            return []

    # ── Private helpers ───────────────────────────────────────────────────────

    def _analyze_known_funders(self) -> list[dict]:
        """
        Analyzes the org's known funders to identify which ones
        may have upcoming grant cycles based on their history.

        Returns:
            List of prior funder activity dictionaries.
        """
        activity = []

        for funder in self.profile.known_funders:
            entry = {
                "funder_name":       funder.name,
                "last_award_year":   funder.last_award_year,
                "last_award_amount": funder.last_award_amount,
                "funder_type":       funder.funder_type.value,
                "notes":             funder.notes or "",
                "status":            self._assess_funder_status(funder),
                "recommended_action": self._recommend_funder_action(funder),
            }
            activity.append(entry)

        return activity

    def _assess_funder_status(self, funder: KnownFunder) -> str:
        """
        Assesses the current status of a known funder relationship.

        Args:
            funder: KnownFunder from the org profile.

        Returns:
            Status string describing the relationship.
        """
        if not funder.last_award_year:
            return "unknown"

        years_since = date.today().year - funder.last_award_year

        if years_since == 0:
            return "active_current_year"
        elif years_since == 1:
            return "active_last_year"
        elif years_since <= 3:
            return "recent_lapsed"
        else:
            return "dormant"

    def _recommend_funder_action(self, funder: KnownFunder) -> str:
        """
        Recommends an action for a known funder based on
        their giving history and relationship status.

        Args:
            funder: KnownFunder from the org profile.

        Returns:
            Recommended action string.
        """
        status = self._assess_funder_status(funder)

        actions = {
            "active_current_year": (
                "Maintain relationship — send impact report and "
                "stewardship update within 30 days."
            ),
            "active_last_year": (
                "High priority renewal — confirm this year's "
                "deadline and begin application preparation."
            ),
            "recent_lapsed": (
                "Re-engagement priority — reach out to program "
                "officer to reconnect and share recent impact."
            ),
            "dormant": (
                "Research funder's current priorities before "
                "re-engagement — giving focus may have shifted."
            ),
            "unknown": (
                "Research this funder's current grant cycle "
                "and confirm relationship contact."
            ),
        }

        return actions.get(status, "Review relationship and determine next step.")

    def _find_co_funders(self) -> list[dict]:
        """
        Searches for foundations that fund alongside
        the org's known funders — co-funder relationships
        reveal warm introduction paths.

        Returns:
            List of co-funder relationship dictionaries.
        """
        co_funders = []

        for funder in self.profile.known_funders[:3]:
            queries = [
                f"{funder.name} co-funding partners Chicago housing",
                f"foundations fund alongside {funder.name} nonprofits",
            ]

            for query in queries:
                try:
                    results = self.web_search.run(query)
                    time.sleep(3)

                    for result in results:
                        if not result.funder_name:
                            continue

                        # Skip if already a known funder
                        known_names = [
                            f.name.lower()
                            for f in self.profile.known_funders
                        ]
                        if result.funder_name.lower() in known_names:
                            continue

                        co_funders.append({
                            "co_funder_name":    result.funder_name,
                            "connected_through": funder.name,
                            "source":            result.source_url or "",
                            "notes": (
                                f"Found as potential co-funder alongside "
                                f"{funder.name} via web research."
                            ),
                        })

                except Exception as e:
                    print(f"[RelationshipMap] Co-funder search error: {e}")
                    continue

        # Deduplicate by co-funder name
        seen_names = set()
        unique     = []
        for cf in co_funders:
            name = cf["co_funder_name"].lower()
            if name not in seen_names:
                seen_names.add(name)
                unique.append(cf)

        return unique[:10]

    def _identify_warm_paths(self) -> list[dict]:
        """
        Identifies foundations with strong mission alignment
        that have not yet funded the org — warm cultivation targets.

        These are not open RFPs. They are strategic prospects
        the development team should build relationships with now.

        Returns:
            List of warm path prospect dictionaries.
        """
        warm_paths  = []
        city        = self.profile.geography.city
        programs    = self.profile.program_areas[:3]

        queries = []
        for program in programs:
            queries.append(
                f"foundation grants {program.value.replace('_',' ')} "
                f"{city} women 2025 2026"
            )

        for query in queries[:3]:
            try:
                results = self.form_990.run(query)
                time.sleep(1)

                for result in results:
                    known_names = [
                        f.name.lower()
                        for f in self.profile.known_funders
                    ]
                    if result.funder_name.lower() in known_names:
                        continue

                    warm_paths.append({
                        "funder_name":        result.funder_name,
                        "geographic_focus":   result.geographic_focus or city,
                        "potential_award":    result.award_range_display,
                        "source":             result.source_url or "",
                        "alignment_basis":    (
                            f"IRS 990 data shows giving patterns aligned "
                            f"with {self.profile.org_short_name} programs. "
                            f"No current open RFP — cultivation target."
                        ),
                        "recommended_action": (
                            "Research this funder's program officer and "
                            "priorities. Request an introductory meeting "
                            "or send a brief letter of introduction."
                        ),
                    })

            except Exception as e:
                print(f"[RelationshipMap] Warm path search error: {e}")
                continue

        return warm_paths[:15]

    def _find_timing_signals(self) -> list[dict]:
        """
        Identifies strategic timing signals — funders whose
        historical patterns suggest an upcoming grant cycle.

        Returns:
            List of timing signal dictionaries.
        """
        signals = []

        current_month = date.today().month

        # Known funders whose cycles typically open in the next 90 days
        for funder in self.profile.known_funders:
            if not funder.last_award_year:
                continue

            years_since = date.today().year - funder.last_award_year
            if years_since <= 2:
                signals.append({
                    "funder_name":    funder.name,
                    "signal_type":    "annual_cycle_likely",
                    "last_award":     funder.last_award_year,
                    "last_amount":    (
                        f"${funder.last_award_amount:,}"
                        if funder.last_award_amount else "Unknown"
                    ),
                    "recommended_action": (
                        f"Monitor {funder.name}'s website for RFP "
                        f"announcement. Based on prior award history, "
                        f"a new cycle may open within 90 days. "
                        f"Contact program officer now to confirm timeline."
                    ),
                })

        # Add general seasonal signals
        if current_month in [1, 2]:
            signals.append({
                "funder_name":    "General — Spring Cycle Funders",
                "signal_type":    "seasonal_spring_cycle",
                "last_award":     None,
                "last_amount":    None,
                "recommended_action": (
                    "Many foundations open spring grant cycles in "
                    "February-March. Review the full watch list and "
                    "identify which funders typically have spring deadlines."
                ),
            })
        elif current_month in [8, 9]:
            signals.append({
                "funder_name":    "General — Fall Cycle Funders",
                "signal_type":    "seasonal_fall_cycle",
                "last_award":     None,
                "last_amount":    None,
                "recommended_action": (
                    "Many foundations open fall grant cycles in "
                    "September-October. Begin preparing applications "
                    "and confirm deadlines with program officers."
                ),
            })

        return signals

    def _build_summary(self, report: dict) -> str:
        """
        Builds a plain-English summary of the relationship map.

        Args:
            report: The full report dictionary.

        Returns:
            Summary string.
        """
        warm_count   = len(report["warm_paths"])
        co_count     = len(report["co_funders"])
        timing_count = len(report["timing_signals"])
        prior_count  = len(report["prior_funder_activity"])

        return (
            f"Relationship mapping run complete for {self.profile.org_name}. "
            f"Analysis covered {prior_count} known funder relationships, "
            f"identified {warm_count} warm path cultivation targets, "
            f"found {co_count} potential co-funder connections, and "
            f"surfaced {timing_count} strategic timing signals. "
            f"See full report for recommended actions on each finding."
        )

    def _save_report(self, report: dict) -> str:
        """
        Saves the relationship map report to disk as JSON.

        Args:
            report: The full report dictionary.

        Returns:
            Path to the saved report file.
        """
        report_dir = Path("outputs") / self._org_slug()
        report_dir.mkdir(parents=True, exist_ok=True)

        timestamp   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        report_path = report_dir / f"relationship_map_{timestamp}.json"

        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        return str(report_path)

    def _org_slug(self) -> str:
        """Returns a filesystem-safe org name slug."""
        return (
            self.profile.org_short_name
            .lower()
            .replace(" ", "_")
            .replace("'", "")
        )

    def _should_run(self) -> bool:
        """
        Checks whether enough time has passed since the last
        relationship map run.

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

    def _print_footer(self, report: dict, elapsed: float) -> None:
        """Prints a formatted cycle completion message."""
        print(f"\n{'='*60}")
        print(f"  RELATIONSHIP MAPPING COMPLETE")
        print(f"  Warm paths identified:    {len(report['warm_paths'])}")
        print(f"  Co-funders found:         {len(report['co_funders'])}")
        print(f"  Timing signals:           {len(report['timing_signals'])}")
        print(f"  Prior funder analysis:    {len(report['prior_funder_activity'])}")
        print(f"  Run time: {round(elapsed, 1)} seconds")
        print(f"{'='*60}\n")