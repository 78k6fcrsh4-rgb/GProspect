"""
run_agent.py
------------
CLI entry point for the Grant Prospecting AI Agent.

This is the single command that runs the entire prospecting pipeline
from start to finish for any nonprofit organization.

Usage:
    # Run with default settings
    python3 run_agent.py --profile profiles/deborah_place.json

    # Run with more queries for a deeper search
    python3 run_agent.py --profile profiles/deborah_place.json --queries 20

    # Run a targeted search for one program area
    python3 run_agent.py --profile profiles/deborah_place.json --program workforce_development

    # Run without AI scoring (faster, for testing search only)
    python3 run_agent.py --profile profiles/deborah_place.json --no-scoring

    # Run with custom search terms
    python3 run_agent.py --profile profiles/deborah_place.json --search "MacArthur Foundation open grants 2026"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from agent.profile import OrgProfile
from agent.loop import AgentLoop
from output.formatter import ResultFormatter
from output.exporter import ResultExporter


def parse_args() -> argparse.Namespace:
    """
    Parses command line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description="Grant Prospecting AI Agent — finds open, actionable grants for nonprofits",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 run_agent.py --profile profiles/deborah_place.json
  python3 run_agent.py --profile profiles/deborah_place.json --queries 20
  python3 run_agent.py --profile profiles/deborah_place.json --program workforce_development
  python3 run_agent.py --profile profiles/deborah_place.json --search "Polk Bros Foundation open grants 2026"
        """
    )

    parser.add_argument(
        "--profile",
        type     = str,
        required = True,
        help     = "Path to the org profile JSON file e.g. profiles/deborah_place.json"
    )

    parser.add_argument(
        "--queries",
        type    = int,
        default = 10,
        help    = "Number of search queries to run (default: 10, more = deeper search)"
    )

    parser.add_argument(
        "--program",
        type    = str,
        default = None,
        help    = "Run a targeted search for one program area only e.g. workforce_development"
    )

    parser.add_argument(
        "--search",
        type    = str,
        default = None,
        help    = "Run a single custom search query e.g. 'MacArthur Foundation open grants 2026'"
    )

    parser.add_argument(
        "--no-scoring",
        action  = "store_true",
        default = False,
        help    = "Skip AI scoring — returns unscored filtered results (faster, lower API cost)"
    )

    parser.add_argument(
        "--output-dir",
        type    = str,
        default = "outputs",
        help    = "Output directory for results files (default: outputs)"
    )

    parser.add_argument(
        "--csv-only",
        action  = "store_true",
        default = False,
        help    = "Export CSV only, skip Excel export"
    )

    return parser.parse_args()


def print_banner(profile: OrgProfile) -> None:
    """
    Prints a startup banner showing the agent configuration.

    Args:
        profile: The loaded org profile.
    """
    print()
    print("=" * 65)
    print("  GRANT PROSPECTING AI AGENT")
    print("  AI for Good — P33 Chicago")
    print("=" * 65)
    print(f"  Organization:  {profile.org_name}")
    print(f"  Location:      {profile.geography.city}, {profile.geography.state}")
    print(f"  Programs:      {len(profile.program_areas)} active program areas")
    print(f"  Grant range:   ${profile.budget.request_floor:,} – ${profile.budget.request_ceiling:,}")
    print(f"  Federal excl.: {'Yes' if profile.settings.exclude_federal else 'No'}")
    print(f"  Run started:   {datetime.now().strftime('%B %d, %Y at %H:%M:%S')}")
    print("=" * 65)
    print()


def print_completion(
    results:      list,
    csv_path:     str,
    excel_path:   str,
    summary_path: str
) -> None:
    """
    Prints a completion message with output file locations.

    Args:
        results:      Final ranked results list.
        csv_path:     Path to the CSV export.
        excel_path:   Path to the Excel export.
        summary_path: Path to the run summary.
    """
    print()
    print("=" * 65)
    print("  RUN COMPLETE")
    print("=" * 65)
    print(f"  Opportunities found: {len(results)}")
    print()
    print("  Output files:")
    print(f"    CSV:     {csv_path}")
    if excel_path:
        print(f"    Excel:   {excel_path}")
    print(f"    Summary: {summary_path}")
    print("=" * 65)
    print()

    if results:
        print("  TOP RESULTS:")
        for opp in results[:3]:
            score_str = f"{opp.score_final:.2f}" if opp.score_final else "Unscored"
            print(f"    [{score_str}] {opp.funder_name}")
            print(f"           {opp.program_name[:55]}")
            if opp.application_deadline:
                print(f"           Deadline: {opp.application_deadline} ({opp.days_until_deadline} days)")
        print()


def main() -> int:
    """
    Main entry point for the CLI.

    Returns:
        Exit code: 0 for success, 1 for error.
    """
    args = parse_args()

    # ── Load and validate the org profile ────────────────────────────────────
    profile_path = Path(args.profile)
    if not profile_path.exists():
        print(f"Error: Profile file not found: '{profile_path}'")
        print(f"Check the path and try again.")
        return 1

    try:
        profile = OrgProfile.from_json(profile_path)
    except Exception as e:
        print(f"Error loading profile: {e}")
        return 1

    # ── Print startup banner ──────────────────────────────────────────────────
    print_banner(profile)

    # ── Initialize components ─────────────────────────────────────────────────
    loop      = AgentLoop(profile)
    formatter = ResultFormatter(profile)
    exporter  = ResultExporter(profile, output_dir=args.output_dir)

    # ── Determine search mode ─────────────────────────────────────────────────
    custom_queries = None

    if args.search:
        # Single custom search query
        custom_queries = [args.search]
        print(f"Search mode: Custom query")
        print(f"Query: '{args.search}'")
        print()

    elif args.program:
        # Targeted program area search
        print(f"Search mode: Targeted — {args.program}")
        print()
        results = loop.run_targeted(
            program_area = args.program,
            max_queries  = args.queries
        )

        # Skip to export
        if not results and not args.no_scoring:
            print("No results found for this program area.")
            return 0

        formatted    = formatter.format_all(results)
        csv_path     = exporter.export_csv(formatted)
        excel_path   = None if args.csv_only else exporter.export_excel(formatted)
        summary_path = exporter.export_run_summary(formatted)
        print_completion(results, csv_path, excel_path, summary_path)
        return 0

    # ── Run the full pipeline ─────────────────────────────────────────────────
    print(f"Search mode: Full pipeline — {args.queries} queries")
    print()

    results = loop.run(
        max_queries    = args.queries,
        custom_queries = custom_queries,
        skip_scoring   = args.no_scoring,
    )

    # ── Format and export results ─────────────────────────────────────────────
    if not results:
        print("No opportunities found in this run.")
        print("Try increasing --queries or using --search with a specific funder name.")
        return 0

    formatted    = formatter.format_all(results)
    csv_path     = exporter.export_csv(formatted)
    excel_path   = None if args.csv_only else exporter.export_excel(formatted)
    summary_path = exporter.export_run_summary(
        formatted,
        raw_count      = len(results),
        filtered_count = len(results)
    )

    print_completion(results, csv_path, excel_path, summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())