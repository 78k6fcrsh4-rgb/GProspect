"""
scoring/eligibility.py
----------------------
EligibilityFilter — applies hard exclusion rules to grant opportunities
before they reach the AI scoring engine.

This filter runs BEFORE the scorer. It removes opportunities that
clearly do not qualify based on objective rules — no AI needed.

Rules are driven entirely by the org profile settings. The filter
never has hardcoded org-specific logic. It reads the profile and
applies whatever rules are configured there.

Why filter before scoring:
- Saves Claude API calls on clearly ineligible opportunities
- Keeps results focused on genuinely actionable opportunities
- Makes the scoring engine more accurate by reducing noise
- Enforces the org's strategic priorities (e.g. no federal grants)

Usage:
    from scoring.eligibility import EligibilityFilter
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    ef      = EligibilityFilter(profile)

    # Filter a list of opportunities
    clean   = ef.filter(opportunities)

    # Check a single opportunity
    result  = ef.check(opportunity)
    print(result.passed)
    print(result.reason)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from agent.profile import OrgProfile, FunderType
from tools.base_tool import GrantOpportunity, OpportunityStatus


# ─────────────────────────────────────────────────────────────────────────────
# EligibilityResult
# Returned by check() for every opportunity evaluated.
# Tells the caller whether the opportunity passed and exactly why not.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EligibilityResult:
    """
    The result of an eligibility check on a single opportunity.

    Attributes:
        passed:      True if the opportunity passed all checks.
        reason:      Why it was rejected, or 'Passed all eligibility checks'
        rule:        Which specific rule triggered the rejection.
        opportunity: The opportunity that was checked.
    """
    passed:      bool
    reason:      str
    rule:        str
    opportunity: GrantOpportunity

    def __str__(self) -> str:
        status = "PASSED" if self.passed else f"REJECTED ({self.rule})"
        return f"[{status}] {self.opportunity.funder_name} — {self.opportunity.program_name}: {self.reason}"


class EligibilityFilter:
    """
    Applies hard exclusion rules to grant opportunities.

    All rules are driven by the org profile — no hardcoded
    org-specific logic anywhere in this class.

    Rules applied in order:
    1. Status must be OPEN
    2. Deadline must not have passed
    3. Deadline must be at least deadline_floor_days away
    4. Deadline must be no more than deadline_ceiling_days away
    5. Funder type must not be excluded
    6. Funder name must not be on the exclusion list
    7. Award size must overlap with org's request range
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the filter with the org profile.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile            = profile
        self.today              = date.today()
        self.excluded_types     = profile.get_active_funder_type_exclusions()
        self.excluded_names     = [
            name.lower().strip()
            for name in profile.funder_exclusions
        ]
        self.deadline_floor     = profile.settings.deadline_floor_days
        self.deadline_ceiling   = profile.settings.deadline_ceiling_days
        self.budget_floor       = profile.budget.request_floor
        self.budget_ceiling     = profile.budget.request_ceiling

    def filter(
        self,
        opportunities: list[GrantOpportunity]
    ) -> list[GrantOpportunity]:
        """
        Filters a list of opportunities, returning only those that
        pass all eligibility checks.

        Args:
            opportunities: List of GrantOpportunity objects to filter.

        Returns:
            List of opportunities that passed all eligibility checks.
        """
        passed  = []
        failed  = []

        for opp in opportunities:
            result = self.check(opp)
            if result.passed:
                passed.append(opp)
            else:
                failed.append(result)

        # Log summary
        print(
            f"[EligibilityFilter] {len(opportunities)} in → "
            f"{len(passed)} passed, {len(failed)} rejected"
        )

        # Log rejection reasons for debugging
        if failed:
            rejection_counts: dict[str, int] = {}
            for r in failed:
                rejection_counts[r.rule] = rejection_counts.get(r.rule, 0) + 1
            for rule, count in sorted(rejection_counts.items(), key=lambda x: -x[1]):
                print(f"[EligibilityFilter]   {count}x rejected by: {rule}")

        return passed

    def check(self, opp: GrantOpportunity) -> EligibilityResult:
        """
        Runs all eligibility checks on a single opportunity.

        Checks are applied in order. The first failed check
        immediately rejects the opportunity — subsequent checks
        are not run. This is the most efficient approach.

        Args:
            opp: A GrantOpportunity object to evaluate.

        Returns:
            EligibilityResult with passed=True if all checks pass,
            or passed=False with the rejection reason and rule.
        """

        # ── Rule 1: Status must be OPEN ───────────────────────────────────────
        if opp.status != OpportunityStatus.OPEN:
            return EligibilityResult(
                passed      = False,
                reason      = f"Opportunity status is '{opp.status.value}' — only OPEN opportunities are included.",
                rule        = "STATUS_NOT_OPEN",
                opportunity = opp
            )

        # ── Rule 2: Deadline must not have passed ─────────────────────────────
        if opp.application_deadline is not None:
            if opp.application_deadline < self.today:
                return EligibilityResult(
                    passed      = False,
                    reason      = f"Application deadline {opp.application_deadline} has already passed.",
                    rule        = "DEADLINE_PASSED",
                    opportunity = opp
                )

        # ── Rule 3: Deadline must be far enough away to apply ─────────────────
        if opp.days_until_deadline is not None:
            if opp.days_until_deadline < self.deadline_floor:
                return EligibilityResult(
                    passed      = False,
                    reason      = (
                        f"Deadline is in {opp.days_until_deadline} days — "
                        f"minimum required is {self.deadline_floor} days to prepare a competitive application."
                    ),
                    rule        = "DEADLINE_TOO_SOON",
                    opportunity = opp
                )

        # ── Rule 4: Deadline must not be too far away ─────────────────────────
        if opp.days_until_deadline is not None:
            if opp.days_until_deadline > self.deadline_ceiling:
                return EligibilityResult(
                    passed      = False,
                    reason      = (
                        f"Deadline is {opp.days_until_deadline} days away — "
                        f"maximum is {self.deadline_ceiling} days. Too distant to be actionable now."
                    ),
                    rule        = "DEADLINE_TOO_FAR",
                    opportunity = opp
                )

        # ── Rule 5: Funder type must not be excluded ──────────────────────────
        if opp.funder_type and self.excluded_types:
            opp_type_str = opp.funder_type.lower().strip() if isinstance(opp.funder_type, str) else opp.funder_type.value
            for excluded_type in self.excluded_types:
                excluded_str = excluded_type.value if isinstance(excluded_type, FunderType) else excluded_type
                if opp_type_str == excluded_str:
                    return EligibilityResult(
                        passed      = False,
                        reason      = (
                            f"Funder type '{opp_type_str}' is excluded based on "
                            f"this organization's current funding strategy settings."
                        ),
                        rule        = "FUNDER_TYPE_EXCLUDED",
                        opportunity = opp
                    )

        # ── Rule 6: Funder name must not be on exclusion list ─────────────────
        if self.excluded_names:
            funder_lower = opp.funder_name.lower().strip()
            for excluded_name in self.excluded_names:
                if excluded_name in funder_lower:
                    return EligibilityResult(
                        passed      = False,
                        reason      = f"Funder '{opp.funder_name}' is on this organization's exclusion list.",
                        rule        = "FUNDER_NAME_EXCLUDED",
                        opportunity = opp
                    )

        # ── Rule 7: Award size must overlap with org's request range ──────────
        # Only apply this rule if both the opportunity and the org have
        # award range information — do not reject if information is missing
        if opp.award_max is not None and opp.award_max > 0:
            if opp.award_max < self.budget_floor:
                return EligibilityResult(
                    passed      = False,
                    reason      = (
                        f"Maximum award of ${opp.award_max:,} is below this organization's "
                        f"minimum grant request of ${self.budget_floor:,}."
                    ),
                    rule        = "AWARD_TOO_SMALL",
                    opportunity = opp
                )

        if opp.award_min is not None and opp.award_min > 0:
            if opp.award_min > self.budget_ceiling:
                return EligibilityResult(
                    passed      = False,
                    reason      = (
                        f"Minimum award of ${opp.award_min:,} exceeds this organization's "
                        f"maximum grant request of ${self.budget_ceiling:,}."
                    ),
                    rule        = "AWARD_TOO_LARGE",
                    opportunity = opp
                )

        # ── All checks passed ─────────────────────────────────────────────────
        return EligibilityResult(
            passed      = True,
            reason      = "Passed all eligibility checks.",
            rule        = "PASSED",
            opportunity = opp
        )

    def get_exclusion_summary(self) -> str:
        """
        Returns a human-readable summary of the active exclusion rules.
        Used in logs and the agent startup sequence.
        """
        excluded_type_names = [
            t.value if isinstance(t, FunderType) else t
            for t in self.excluded_types
        ]
        return (
            f"EligibilityFilter for: {self.profile.org_short_name}\n"
            f"  Excluded funder types: {', '.join(excluded_type_names) or 'none'}\n"
            f"  Excluded funder names: {', '.join(self.profile.funder_exclusions) or 'none'}\n"
            f"  Deadline floor: {self.deadline_floor} days\n"
            f"  Deadline ceiling: {self.deadline_ceiling} days\n"
            f"  Budget range: ${self.budget_floor:,} – ${self.budget_ceiling:,}\n"
        )