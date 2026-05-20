"""
agent/scheduler.py
------------------
AgentScheduler — runs all three operating cycles automatically
on their configured schedules.

Schedules:
    Monitoring cycle:         Daily at 7:00 AM
    Discovery cycle:          Every Monday at 6:00 AM
    Relationship map cycle:   1st of every month at 5:00 AM

The scheduler runs as a background process. Once started it
keeps running until manually stopped (Ctrl+C) or the process
is killed.

Usage:
    # Start the scheduler (runs indefinitely)
    python3 -m agent.scheduler --profile profiles/deborah_place.json

    # Or import and run programmatically
    from agent.scheduler import AgentScheduler
    from agent.profile import OrgProfile

    profile   = OrgProfile.from_json("profiles/deborah_place.json")
    scheduler = AgentScheduler(profile)
    scheduler.start()
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime
from typing import Optional

import schedule

from agent.profile import OrgProfile
from cycles.monitoring import MonitoringCycle
from cycles.discovery import DiscoveryCycle
from cycles.relationship_map import RelationshipMapCycle


class AgentScheduler:
    """
    Schedules and runs all three operating cycles automatically.

    Uses the 'schedule' library to run cycles at configured times.
    Runs in the foreground — keep the terminal open or run as a
    background service using systemd or screen.

    The scheduler respects each cycle's built-in timing guards
    so even if it fires at the wrong time, the cycle itself
    will check whether it should actually run.
    """

    def __init__(
        self,
        profile:             OrgProfile,
        monitoring_time:     str = "07:00",
        discovery_day:       str = "monday",
        discovery_time:      str = "06:00",
        relationship_map_day: int = 1,
        relationship_map_time: str = "05:00",
    ) -> None:
        """
        Initialize the scheduler with the org profile and cycle times.

        Args:
            profile:               Loaded and validated OrgProfile.
            monitoring_time:       Daily monitoring time (HH:MM format).
            discovery_day:         Day of week for discovery cycle.
            discovery_time:        Time for discovery cycle (HH:MM).
            relationship_map_day:  Day of month for relationship map.
            relationship_map_time: Time for relationship map (HH:MM).
        """
        self.profile = profile
        self.running = False

        # Initialize all three cycles
        self.monitoring_cycle     = MonitoringCycle(profile)
        self.discovery_cycle      = DiscoveryCycle(profile)
        self.relationship_cycle   = RelationshipMapCycle(profile)

        # Store schedule configuration
        self.monitoring_time      = monitoring_time
        self.discovery_day        = discovery_day
        self.discovery_time       = discovery_time
        self.relationship_map_day  = relationship_map_day
        self.relationship_map_time = relationship_map_time

        # Track last run times for logging
        self.last_monitoring_run     = None
        self.last_discovery_run      = None
        self.last_relationship_run   = None
        self.total_runs              = 0

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT,  self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def start(self) -> None:
        """
        Starts the scheduler and runs indefinitely.

        Sets up all three cycle schedules and enters the main
        loop. Blocks until Ctrl+C or process termination.

        The scheduler checks every 60 seconds whether any
        scheduled job is due to run.
        """
        self._print_banner()
        self._setup_schedules()
        self.running = True

        print(f"\n[Scheduler] Running. Press Ctrl+C to stop.\n")

        while self.running:
            schedule.run_pending()
            time.sleep(60)

        print("\n[Scheduler] Stopped.")

    def run_all_now(self) -> dict:
        """
        Runs all three cycles immediately regardless of schedule.

        Useful for:
        - Initial setup to populate all outputs immediately
        - Testing that all cycles work
        - Manual full refresh when needed

        Returns:
            Dictionary with results from each cycle.
        """
        print("\n[Scheduler] Running all cycles now...")
        results = {}

        print("\n[Scheduler] Running monitoring cycle...")
        results["monitoring"] = self.monitoring_cycle.run(force=True)

        print("\n[Scheduler] Running discovery cycle...")
        results["discovery"] = self.discovery_cycle.run(force=True)

        print("\n[Scheduler] Running relationship map...")
        results["relationship_map"] = self.relationship_cycle.run(force=True)

        self.total_runs += 3
        print("\n[Scheduler] All cycles complete.")
        return results

    def get_next_run_times(self) -> dict:
        """
        Returns the next scheduled run time for each cycle.

        Returns:
            Dictionary with next run times for each cycle.
        """
        jobs = schedule.get_jobs()
        next_times = {}

        for job in jobs:
            next_times[str(job.job_func.__name__)] = str(job.next_run)

        return next_times

    def print_status(self) -> None:
        """
        Prints the current scheduler status and next run times.
        """
        print(f"\n{'='*60}")
        print(f"  AGENT SCHEDULER STATUS")
        print(f"  Organization: {self.profile.org_name}")
        print(f"  Running: {self.running}")
        print(f"  Total runs completed: {self.total_runs}")
        print(f"{'='*60}")
        print(f"  CYCLE SCHEDULES:")
        print(f"  Monitoring:        Daily at {self.monitoring_time}")
        print(f"  Discovery:         Every {self.discovery_day.capitalize()} at {self.discovery_time}")
        print(f"  Relationship map:  Day {self.relationship_map_day} of each month at {self.relationship_map_time}")
        print(f"{'='*60}")

        if self.last_monitoring_run:
            print(f"  Last monitoring run:     {self.last_monitoring_run}")
        if self.last_discovery_run:
            print(f"  Last discovery run:      {self.last_discovery_run}")
        if self.last_relationship_run:
            print(f"  Last relationship run:   {self.last_relationship_run}")
        print(f"{'='*60}\n")

    # ── Private helpers ───────────────────────────────────────────────────────

    def _setup_schedules(self) -> None:
        """
        Registers all three cycles with the schedule library.
        """
        # Daily monitoring cycle
        schedule.every().day.at(self.monitoring_time).do(
            self._run_monitoring
        )

        # Weekly discovery cycle
        getattr(schedule.every(), self.discovery_day).at(
            self.discovery_time
        ).do(self._run_discovery)

        # Monthly relationship map
        # The schedule library does not support monthly natively
        # so we run daily and let the cycle's own timing guard
        # handle skipping if it ran recently
        schedule.every().day.at(self.relationship_map_time).do(
            self._run_relationship_map
        )

        print(f"[Scheduler] Schedules configured:")
        print(f"  Monitoring:      Daily at {self.monitoring_time}")
        print(f"  Discovery:       Every {self.discovery_day} at {self.discovery_time}")
        print(f"  Relationship map: Daily at {self.relationship_map_time} (runs monthly)")

    def _run_monitoring(self) -> None:
        """
        Wrapper that runs the monitoring cycle and logs completion.
        """
        print(f"\n[Scheduler] Firing monitoring cycle — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        try:
            self.monitoring_cycle.run()
            self.last_monitoring_run = datetime.now().strftime("%Y-%m-%d %H:%M")
            self.total_runs += 1
        except Exception as e:
            print(f"[Scheduler] Monitoring cycle error: {e}")

    def _run_discovery(self) -> None:
        """
        Wrapper that runs the discovery cycle and logs completion.
        """
        print(f"\n[Scheduler] Firing discovery cycle — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        try:
            self.discovery_cycle.run()
            self.last_discovery_run = datetime.now().strftime("%Y-%m-%d %H:%M")
            self.total_runs += 1
        except Exception as e:
            print(f"[Scheduler] Discovery cycle error: {e}")

    def _run_relationship_map(self) -> None:
        """
        Wrapper that runs the relationship map cycle and logs completion.
        The cycle's own timing guard ensures it only runs monthly.
        """
        print(f"\n[Scheduler] Checking relationship map — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        try:
            self.relationship_cycle.run()
            self.last_relationship_run = datetime.now().strftime("%Y-%m-%d %H:%M")
            self.total_runs += 1
        except Exception as e:
            print(f"[Scheduler] Relationship map error: {e}")

    def _handle_shutdown(self, signum, frame) -> None:
        """
        Handles graceful shutdown on Ctrl+C or SIGTERM.

        Args:
            signum: Signal number.
            frame:  Current stack frame.
        """
        print(f"\n[Scheduler] Shutdown signal received — stopping gracefully...")
        self.running = False
        schedule.clear()

    def _print_banner(self) -> None:
        """Prints the scheduler startup banner."""
        print(f"\n{'='*60}")
        print(f"  GRANT PROSPECTING AGENT — AUTONOMOUS MODE")
        print(f"  AI for Good — P33 Chicago")
        print(f"{'='*60}")
        print(f"  Organization: {self.profile.org_name}")
        print(f"  Started:      {datetime.now().strftime('%B %d, %Y at %H:%M:%S')}")
        print(f"{'='*60}")


def main():
    """
    CLI entry point for running the scheduler.

    Usage:
        python3 -m agent.scheduler --profile profiles/deborah_place.json
        python3 -m agent.scheduler --profile profiles/deborah_place.json --run-now
    """
    parser = argparse.ArgumentParser(
        description="Grant Prospecting Agent Scheduler — runs cycles automatically"
    )
    parser.add_argument(
        "--profile",
        required = True,
        help     = "Path to org profile JSON file"
    )
    parser.add_argument(
        "--run-now",
        action  = "store_true",
        default = False,
        help    = "Run all cycles immediately then start scheduler"
    )
    parser.add_argument(
        "--monitoring-time",
        default = "07:00",
        help    = "Daily monitoring time HH:MM (default: 07:00)"
    )
    parser.add_argument(
        "--discovery-day",
        default = "monday",
        help    = "Day for weekly discovery cycle (default: monday)"
    )

    args    = parser.parse_args()
    profile = OrgProfile.from_json(args.profile)

    scheduler = AgentScheduler(
        profile          = profile,
        monitoring_time  = args.monitoring_time,
        discovery_day    = args.discovery_day,
    )

    scheduler.print_status()

    if args.run_now:
        print("[Scheduler] --run-now flag set — running all cycles immediately...")
        scheduler.run_all_now()

    scheduler.start()


if __name__ == "__main__":
    main()