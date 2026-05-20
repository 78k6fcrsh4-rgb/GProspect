"""
scoring/scorer.py
-----------------
GrantScorer — scores grant opportunities against the org profile
using Claude AI to produce the 1-5 qualification matrix.

Criteria (1-5 scale):
    1. Geographic Alignment       (weight: 1x)
    2. Population Served          (weight: 1x)
    3. Budget Fit                 (weight: 2x)
    4. Timeline Feasibility       (weight: 1x)

Composite score = (geo + pop + (budget * 2) + timeline) / 5

Deadline proximity multiplier:
    < 14 days:  * 1.5
    < 30 days:  * 1.3
    < 60 days:  * 1.1
    < 90 days:  * 1.0
    > 90 days:  * 0.9
"""

from __future__ import annotations

import json
import os
import time
from datetime import date
from typing import Optional

import anthropic
from dotenv import load_dotenv

from agent.profile import OrgProfile
from tools.base_tool import GrantOpportunity

load_dotenv()

WEIGHT_GEOGRAPHIC   = 1.0
WEIGHT_POPULATION   = 1.0
WEIGHT_BUDGET       = 2.0
WEIGHT_TIMELINE     = 1.0
WEIGHT_TOTAL        = WEIGHT_GEOGRAPHIC + WEIGHT_POPULATION + WEIGHT_BUDGET + WEIGHT_TIMELINE

DEADLINE_MULTIPLIERS = [
    (14,  1.5),
    (30,  1.3),
    (60,  1.1),
    (90,  1.0),
    (365, 0.9),
]


class GrantScorer:
    """
    Scores grant opportunities against the org profile using Claude AI.
    Implements the 1-5 qualification matrix Deborah's Place already uses.
    """

    REQUEST_DELAY_SECONDS = 5.0

    def __init__(self, profile: OrgProfile) -> None:
        self.profile = profile
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment.")
        self.client = anthropic.Anthropic(api_key=api_key)

    def score_all(
        self,
        opportunities: list[GrantOpportunity],
        min_score: Optional[float] = None
    ) -> list[GrantOpportunity]:
        """
        Scores all opportunities and returns them sorted by final score.

        Args:
            opportunities: List of GrantOpportunity objects that passed
                          the eligibility filter.
            min_score:    Optional minimum final score threshold.

        Returns:
            Scored list sorted by final score descending.
        """
        if not opportunities:
            return []

        threshold = min_score or self.profile.settings.min_composite_score
        scored    = []

        print(f"[GrantScorer] Scoring {len(opportunities)} opportunities...")

        for i, opp in enumerate(opportunities, 1):
            print(f"[GrantScorer] Scoring {i}/{len(opportunities)}: {opp.funder_name[:40]}")
            scored_opp = self.score_one(opp)

            if scored_opp.score_final is not None:
                if scored_opp.score_final >= threshold:
                    scored.append(scored_opp)
                else:
                    print(
                        f"[GrantScorer] Below threshold "
                        f"({scored_opp.score_final:.2f} < {threshold}) — excluded"
                    )

            time.sleep(self.REQUEST_DELAY_SECONDS)

        scored.sort(key=lambda x: x.score_final or 0, reverse=True)
        print(f"[GrantScorer] {len(scored)} opportunities above threshold after scoring")
        return scored

    def score_one(self, opp: GrantOpportunity) -> GrantOpportunity:
        """
        Scores a single opportunity using Claude AI.

        Args:
            opp: A GrantOpportunity that passed eligibility filtering.

        Returns:
            The same GrantOpportunity with scoring fields populated.
        """
        prompt = self._build_scoring_prompt(opp)

        try:
            response = self.client.messages.create(
                model      = "claude-haiku-4-5-20251001",
                max_tokens = 500,
                messages   = [{"role": "user", "content": prompt}]
            )

            raw_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    raw_text += block.text

            scores = self._parse_scores(raw_text)
            if scores:
                opp = self._apply_scores(opp, scores)

        except Exception as e:
            print(f"[GrantScorer] Error scoring '{opp.funder_name}': {e}")

        return opp

    def _build_scoring_prompt(self, opp: GrantOpportunity) -> str:
        """
        Builds a concise scoring prompt to stay within token limits.

        Args:
            opp: The opportunity to score.

        Returns:
            Compact prompt string.
        """
        org = self.profile

        if opp.application_deadline:
            deadline_str = (
                f"{opp.application_deadline} "
                f"({opp.days_until_deadline} days away)"
            )
        else:
            deadline_str = "Not specified"

        programs    = ', '.join(
            p.value.replace('_', ' ')
            for p in org.program_areas[:4]
        )
        populations = ', '.join(
            p.value.replace('_', ' ')
            for p in org.populations_served[:4]
        )
        eligibility = (opp.eligibility_requirements or 'Not specified')[:200]
        description = (opp.description or 'Not provided')[:200]

        return f"""Score this grant for {org.org_short_name} ({org.geography.city}, {org.geography.state}).
Org programs: {programs}
Org populations: {populations}
Org grant range: ${org.budget.request_floor:,}-${org.budget.request_ceiling:,}

Grant: {opp.funder_name} — {opp.program_name}
Geographic focus: {opp.geographic_focus or 'Not specified'}
Award: {opp.award_range_display}
Deadline: {deadline_str}
Eligibility: {eligibility}
Description: {description}

Rate 1-5 on each criterion. Budget fit is most important (2x weight).
1=poor fit, 3=average, 5=excellent fit.

Return ONLY this JSON:
{{"score_geographic":<1-5>,"reason_geographic":"<one sentence>","score_population":<1-5>,"reason_population":"<one sentence>","score_budget":<1-5>,"reason_budget":"<one sentence>","score_timeline":<1-5>,"reason_timeline":"<one sentence>"}}"""

    def _parse_scores(self, raw_text: str) -> Optional[dict]:
        """
        Parses Claude's JSON score response.

        Args:
            raw_text: Raw text response from Claude.

        Returns:
            Dictionary of scores and reasons, or None if parsing fails.
        """
        start = raw_text.find("{")
        end   = raw_text.rfind("}")
        if start == -1 or end == -1:
            return None

        json_str = raw_text[start:end + 1]

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    def _apply_scores(
        self,
        opp: GrantOpportunity,
        scores: dict
    ) -> GrantOpportunity:
        """
        Applies parsed scores to the GrantOpportunity object and
        calculates composite and final scores.

        Args:
            opp:    The opportunity to update.
            scores: Dictionary of scores from Claude.

        Returns:
            Updated GrantOpportunity with all scoring fields populated.
        """
        geo      = max(1.0, min(5.0, float(scores.get("score_geographic", 1))))
        pop      = max(1.0, min(5.0, float(scores.get("score_population", 1))))
        budget   = max(1.0, min(5.0, float(scores.get("score_budget", 1))))
        timeline = max(1.0, min(5.0, float(scores.get("score_timeline", 1))))

        composite = (
            (geo      * WEIGHT_GEOGRAPHIC) +
            (pop      * WEIGHT_POPULATION) +
            (budget   * WEIGHT_BUDGET)     +
            (timeline * WEIGHT_TIMELINE)
        ) / WEIGHT_TOTAL

        multiplier = self._get_deadline_multiplier(opp.days_until_deadline)
        final      = round(composite * multiplier, 2)

        opp.score_geographic  = geo
        opp.score_population  = pop
        opp.score_budget      = budget
        opp.score_timeline    = timeline
        opp.score_composite   = round(composite, 2)
        opp.score_final       = final
        opp.reason_geographic = scores.get("reason_geographic", "")
        opp.reason_population = scores.get("reason_population", "")
        opp.reason_budget     = scores.get("reason_budget", "")
        opp.reason_timeline   = scores.get("reason_timeline", "")

        return opp

    def _get_deadline_multiplier(
        self,
        days_until_deadline: Optional[int]
    ) -> float:
        """
        Returns the deadline proximity multiplier.

        Args:
            days_until_deadline: Days until deadline, or None.

        Returns:
            Float multiplier to apply to composite score.
        """
        if days_until_deadline is None:
            return 0.8

        for days_threshold, multiplier in DEADLINE_MULTIPLIERS:
            if days_until_deadline <= days_threshold:
                return multiplier

        return 0.9