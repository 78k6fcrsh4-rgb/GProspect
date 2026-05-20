"""
output/formatter.py
-------------------
ResultFormatter — structures scored GrantOpportunity objects into
the final output format for export and portal display.

Ensures every result contains:
- The Who: funder, program, contact
- The How: deadline, eligibility, award, application method
- Scores: all four criteria with written explanations
- Next action: specific recommended step based on score and deadline

Usage:
    from output.formatter import ResultFormatter
    from agent.profile import OrgProfile

    profile   = OrgProfile.from_json("profiles/deborah_place.json")
    formatter = ResultFormatter(profile)

    formatted = formatter.format_all(scored_opportunities)
    for result in formatted:
        print(result['funder_name'])
        print(result['next_action'])
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from agent.profile import OrgProfile
from tools.base_tool import GrantOpportunity


class ResultFormatter:
    """
    Formats scored GrantOpportunity objects into structured output records.

    Each formatted record is a dictionary containing all fields needed
    for the CSV export, portal display, and LOI drafting.

    The formatter enforces the non-negotiable output standard:
    every record must have The Who and The How complete.
    Records missing critical fields are flagged but still included
    with a note indicating what is missing.
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the formatter with the org profile.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile = profile

    def format_all(
        self,
        opportunities: list[GrantOpportunity]
    ) -> list[dict]:
        """
        Formats all scored opportunities into output records.

        Args:
            opportunities: List of scored GrantOpportunity objects
                          sorted by final score descending.

        Returns:
            List of formatted dictionaries ready for export.
            Maintains the same order as the input list.
        """
        formatted = []
        for rank, opp in enumerate(opportunities, 1):
            record = self.format_one(opp, rank=rank)
            formatted.append(record)

        print(f"[ResultFormatter] Formatted {len(formatted)} opportunities")
        return formatted

    def format_one(
        self,
        opp: GrantOpportunity,
        rank: int = 0
    ) -> dict:
        """
        Formats a single scored opportunity into an output record.

        Builds a flat dictionary with all fields needed for CSV export
        and portal display. Checks completeness of The Who and The How
        and adds appropriate flags and notes.

        Args:
            opp:  A scored GrantOpportunity object.
            rank: The opportunity's rank in the results list (1 = best).

        Returns:
            Dictionary with all formatted fields.
        """
        # ── The Who ───────────────────────────────────────────────────────────
        who_complete = opp.has_complete_who
        how_complete = opp.has_complete_how

        # ── Deadline formatting ───────────────────────────────────────────────
        deadline_display = self._format_deadline(opp)
        days_remaining   = opp.days_until_deadline

        # ── Score formatting ──────────────────────────────────────────────────
        score_display    = self._format_score(opp)

        # ── Next action ───────────────────────────────────────────────────────
        next_action      = self._determine_next_action(opp)

        # ── Completeness flag ─────────────────────────────────────────────────
        completeness_notes = self._check_completeness(opp)

        # ── Prior funder flag ─────────────────────────────────────────────────
        is_prior_funder  = self._check_prior_funder(opp)

        return {
            # Ranking
            "rank":                     rank,

            # THE WHO
            "funder_name":              opp.funder_name,
            "program_name":             opp.program_name,
            "program_officer":          opp.program_officer or "Not listed",
            "funder_website":           opp.funder_website or "Not found",
            "funder_type":              opp.funder_type or "Unknown",
            "who_complete":             "Yes" if who_complete else "Incomplete",

            # THE HOW
            "application_deadline":     deadline_display,
            "days_remaining":           days_remaining if days_remaining is not None else "Unknown",
            "award_range":              opp.award_range_display,
            "award_min":                opp.award_min or "Not specified",
            "award_max":                opp.award_max or "Not specified",
            "application_url":          opp.application_url or "Not found",
            "application_method":       opp.application_method or "Unknown",
            "eligibility_requirements": opp.eligibility_requirements or "Not specified",
            "required_documents":       self._format_list(opp.required_documents),
            "disqualifying_factors":    self._format_list(opp.disqualifying_factors),
            "how_complete":             "Yes" if how_complete else "Incomplete",

            # Description
            "description":              opp.description or "Not provided",
            "geographic_focus":         opp.geographic_focus or "Not specified",
            "focus_areas":              self._format_list(opp.focus_areas),

            # Scores
            "score_final":              opp.score_final or "Not scored",
            "score_composite":          opp.score_composite or "Not scored",
            "score_geographic":         opp.score_geographic or "Not scored",
            "score_population":         opp.score_population or "Not scored",
            "score_budget":             opp.score_budget or "Not scored",
            "score_timeline":           opp.score_timeline or "Not scored",

            # Score explanations
            "reason_geographic":        opp.reason_geographic or "Not available",
            "reason_population":        opp.reason_population or "Not available",
            "reason_budget":            opp.reason_budget or "Not available",
            "reason_timeline":          opp.reason_timeline or "Not available",

            # Action and metadata
            "next_action":              next_action,
            "is_prior_funder":          "Yes — warm lead" if is_prior_funder else "No",
            "completeness_notes":       completeness_notes,
            "source":                   opp.source_name,
            "source_url":               opp.source_url or "Not found",
            "date_found":               opp.date_found.strftime("%Y-%m-%d"),
            "org_name":                 self.profile.org_short_name,
        }

    def format_summary(
        self,
        formatted_results: list[dict]
    ) -> str:
        """
        Generates a plain-text summary of the formatted results.

        Used for logging and terminal output after a prospecting run.

        Args:
            formatted_results: List of formatted result dictionaries.

        Returns:
            Multi-line summary string.
        """
        if not formatted_results:
            return "No results to summarize."

        lines = [
            f"\n{'='*60}",
            f"GRANT PROSPECTING RESULTS — {self.profile.org_name}",
            f"Run date: {date.today().strftime('%B %d, %Y')}",
            f"Total opportunities: {len(formatted_results)}",
            f"{'='*60}",
        ]

        for r in formatted_results:
            lines.extend([
                f"\nRank {r['rank']}: {r['funder_name']}",
                f"  Program:   {r['program_name']}",
                f"  Score:     {r['score_final']}/5",
                f"  Deadline:  {r['application_deadline']} ({r['days_remaining']} days)",
                f"  Award:     {r['award_range']}",
                f"  Action:    {r['next_action']}",
                f"  Prior funder: {r['is_prior_funder']}",
            ])

        lines.append(f"\n{'='*60}")
        return "\n".join(lines)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _format_deadline(self, opp: GrantOpportunity) -> str:
        """
        Formats the deadline as a human-readable string.

        Args:
            opp: GrantOpportunity with deadline field.

        Returns:
            Formatted deadline string.
        """
        if opp.application_deadline is None:
            return "No deadline found — verify with funder"
        return opp.application_deadline.strftime("%B %d, %Y")

    def _format_score(self, opp: GrantOpportunity) -> str:
        """
        Formats the final score as a display string.

        Args:
            opp: Scored GrantOpportunity.

        Returns:
            Score display string.
        """
        if opp.score_final is None:
            return "Not scored"
        return f"{opp.score_final:.2f} / 5.00"

    def _determine_next_action(self, opp: GrantOpportunity) -> str:
        """
        Determines the recommended next action based on score and deadline.

        The action is specific and immediately actionable — not generic.
        It takes into account both the opportunity's quality and urgency.

        Args:
            opp: Scored GrantOpportunity.

        Returns:
            Specific recommended next action string.
        """
        days   = opp.days_until_deadline
        score  = opp.score_final or 0

        # No deadline — can not determine urgency
        if days is None:
            if score >= 3.5:
                return "Contact funder directly to confirm deadline and request application materials."
            return "Verify current deadline with funder before investing time."

        # High score opportunities
        if score >= 4.0:
            if days <= 14:
                return "URGENT — Begin application immediately. Deadline is within 2 weeks."
            elif days <= 30:
                return "High priority — Start application this week. Strong fit with approaching deadline."
            elif days <= 60:
                return "Schedule application kickoff within the next 2 weeks."
            elif days <= 90:
                return "Add to active pipeline. Begin research and outline within 30 days."
            else:
                return "Add to watch list. Set reminder to begin application 60 days before deadline."

        # Medium score opportunities
        elif score >= 2.5:
            if days <= 30:
                return "Review eligibility carefully before committing time — moderate fit."
            elif days <= 60:
                return "Research funder priorities further before deciding to apply."
            else:
                return "Monitor for next cycle — consider relationship building with program officer."

        # Lower score opportunities
        else:
            return "Low fit score — review funder priorities before pursuing."

    def _check_prior_funder(self, opp: GrantOpportunity) -> bool:
        """
        Checks whether this funder has previously funded the organization.

        Compares the opportunity's funder name against the org profile's
        known_funders list. Case-insensitive partial match.

        Args:
            opp: GrantOpportunity to check.

        Returns:
            True if this is a prior funder, False otherwise.
        """
        funder_lower = opp.funder_name.lower()
        for known in self.profile.known_funders:
            if known.name.lower() in funder_lower or funder_lower in known.name.lower():
                return True
        return False

    def _check_completeness(self, opp: GrantOpportunity) -> str:
        """
        Checks which required fields are missing from the opportunity.

        Used to flag results that need manual research to complete
        The Who or The How before an application can be submitted.

        Args:
            opp: GrantOpportunity to check.

        Returns:
            Comma-separated list of missing fields, or 'Complete'.
        """
        missing = []

        if not opp.application_deadline:
            missing.append("deadline not verified")
        if not opp.eligibility_requirements:
            missing.append("eligibility requirements not found")
        if not opp.application_url:
            missing.append("application URL not found")
        if not opp.award_max and not opp.award_min:
            missing.append("award amount not specified")
        if not opp.program_officer:
            missing.append("program officer not listed")

        return ", ".join(missing) if missing else "Complete"

    def _format_list(self, items: Optional[list]) -> str:
        """
        Formats a list as a semicolon-separated string for CSV export.

        Args:
            items: List of strings or None.

        Returns:
            Semicolon-separated string or 'Not specified'.
        """
        if not items:
            return "Not specified"
        return "; ".join(str(item) for item in items)