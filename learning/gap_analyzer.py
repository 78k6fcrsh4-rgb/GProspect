"""
learning/gap_analyzer.py
------------------------
GapAnalyzer — uses Claude AI to analyze why a grant was missed
and determine what changes will prevent missing it in the future.

This is the intelligence layer of the learning loop. It takes
a MissedGrantSubmission and produces a structured analysis
describing the gap and recommended fixes.

The analyzer classifies gaps into four types:
    1. source_not_monitored  — the URL was not in the watch list
    2. keyword_gap           — search terms did not cover this area
    3. search_too_narrow     — geographic or program filters too tight
    4. site_structure        — the site layout prevented extraction

Confidence levels:
    high   — agent makes changes automatically
    medium — agent makes changes and notifies admin
    low    — agent flags for admin review, no auto changes

Usage:
    from learning.gap_analyzer import GapAnalyzer
    from learning.feedback import MissedGrantSubmission
    from agent.profile import OrgProfile

    profile  = OrgProfile.from_json("profiles/deborah_place.json")
    analyzer = GapAnalyzer(profile)
    analysis = analyzer.analyze(submission)

    print(analysis["gap_type"])
    print(analysis["confidence"])
    print(analysis["recommended_changes"])
"""

from __future__ import annotations

import json
import os
from typing import Optional

import anthropic
from dotenv import load_dotenv

from agent.profile import OrgProfile
from agent.state import AgentState
from learning.feedback import MissedGrantSubmission

load_dotenv()


class GapAnalyzer:
    """
    Analyzes missed grant submissions to identify why the agent
    missed them and what changes will improve future coverage.

    Uses Claude AI to reason over the submission details and
    the agent's current configuration to produce a structured
    analysis with specific recommended changes.
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the gap analyzer.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile = profile
        self.state   = AgentState(profile)
        api_key      = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment.")
        self.client  = anthropic.Anthropic(api_key=api_key)

    def analyze(self, submission: MissedGrantSubmission) -> dict:
        """
        Analyzes a missed grant submission to identify the coverage gap.

        Steps:
            1. Check if the source URL is in the watch list
            2. Check if relevant keywords exist in the mapper
            3. Use Claude to reason over the full context
            4. Return structured analysis with recommended changes

        Args:
            submission: A MissedGrantSubmission from FeedbackProcessor.

        Returns:
            Dictionary containing:
                - gap_type:            Type of gap identified
                - confidence:          high, medium, or low
                - explanation:         Plain-English explanation
                - recommended_changes: List of specific changes to make
                - source_to_add:       URL to add to watch list if any
                - keywords_to_add:     Keywords to add if any
        """
        print(f"[GapAnalyzer] Analyzing: {submission.funder_name}")

        # ── Step 1: Rule-based checks first ──────────────────────────────────
        rule_analysis = self._run_rule_checks(submission)

        # ── Step 2: AI-powered deep analysis ─────────────────────────────────
        ai_analysis = self._run_ai_analysis(submission, rule_analysis)

        # ── Step 3: Merge and return final analysis ───────────────────────────
        final = self._merge_analysis(rule_analysis, ai_analysis)

        print(
            f"[GapAnalyzer] Gap type: {final['gap_type']} | "
            f"Confidence: {final['confidence']}"
        )

        return final

    # ── Private helpers ───────────────────────────────────────────────────────

    def _run_rule_checks(self, submission: MissedGrantSubmission) -> dict:
        """
        Runs fast rule-based checks before calling the AI.

        Checks whether the source URL is in the watch list and
        whether relevant keywords exist in the keyword mapper.
        These checks are instant and free — no API call needed.

        Args:
            submission: The missed grant submission.

        Returns:
            Dictionary with rule check results.
        """
        watch_list     = self.state.get_watch_list()
        watch_list_urls = {
            s["url"].lower()
            for s in watch_list
        }

        # Check if source URL is monitored
        source_url_lower    = submission.source_url.lower()
        funder_url_lower    = (submission.funder_website or "").lower()
        source_monitored    = (
            source_url_lower in watch_list_urls or
            funder_url_lower in watch_list_urls or
            any(
                source_url_lower.startswith(url) or
                url.startswith(source_url_lower[:20])
                for url in watch_list_urls
            )
        )

        # Check if funder name appears in known sources
        funder_lower        = submission.funder_name.lower()
        funder_in_watchlist = any(
            funder_lower in s["name"].lower() or
            s["name"].lower() in funder_lower
            for s in watch_list
        )

        # Check keyword coverage
        from agent.keyword_mapper import KeywordMapper
        mapper          = KeywordMapper(self.profile)
        all_queries     = mapper.build_search_queries()
        queries_lower   = [q.lower() for q in all_queries]

        funder_words    = funder_lower.split()
        keyword_covered = any(
            any(word in query for word in funder_words if len(word) > 4)
            for query in queries_lower
        )

        return {
            "source_monitored":     source_monitored,
            "funder_in_watchlist":  funder_in_watchlist,
            "keyword_covered":      keyword_covered,
            "watch_list_size":      len(watch_list),
            "total_queries":        len(all_queries),
        }

    def _run_ai_analysis(
        self,
        submission:    MissedGrantSubmission,
        rule_analysis: dict
    ) -> dict:
        """
        Uses Claude to reason over the gap and produce
        specific recommended changes.

        Args:
            submission:    The missed grant submission.
            rule_analysis: Results from the rule-based checks.

        Returns:
            Dictionary with AI analysis results.
        """
        prompt = self._build_analysis_prompt(submission, rule_analysis)

        try:
            response = self.client.messages.create(
                model      = "claude-haiku-4-5-20251001",
                max_tokens = 800,
                messages   = [{"role": "user", "content": prompt}]
            )

            raw_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    raw_text += block.text

            return self._parse_ai_response(raw_text)

        except Exception as e:
            print(f"[GapAnalyzer] AI analysis error: {e}")
            return {
                "gap_type":            "unknown",
                "confidence":          "low",
                "explanation":         f"AI analysis failed: {e}",
                "recommended_changes": [],
                "keywords_to_add":     [],
            }

    def _build_analysis_prompt(
        self,
        submission:    MissedGrantSubmission,
        rule_analysis: dict
    ) -> str:
        """
        Builds the Claude prompt for gap analysis.

        Args:
            submission:    The missed grant submission.
            rule_analysis: Rule check results for context.

        Returns:
            Complete prompt string.
        """
        org = self.profile

        return f"""You are analyzing why a grant prospecting AI agent missed a funding opportunity.

ORGANIZATION: {org.org_name} ({org.geography.city}, {org.geography.state})
Programs: {', '.join(p.value.replace('_',' ') for p in org.program_areas[:4])}

MISSED GRANT:
Funder: {submission.funder_name}
Program: {submission.program_name}
Source URL: {submission.source_url}
Funder website: {submission.funder_website or 'Not provided'}
Notes from staff: {submission.notes or 'None'}

RULE CHECK RESULTS:
Source URL in watch list: {rule_analysis['source_monitored']}
Funder name in watch list: {rule_analysis['funder_in_watchlist']}
Keywords cover this funder: {rule_analysis['keyword_covered']}
Current watch list size: {rule_analysis['watch_list_size']} sources
Current search queries: {rule_analysis['total_queries']} queries

TASK:
Identify why the agent missed this grant and what specific changes
will prevent missing it and similar grants in the future.

Gap types:
- source_not_monitored: The funder's website is not in the watch list
- keyword_gap: Search terms don't include relevant terms for this funder
- search_too_narrow: Geographic or program filters excluded this
- site_structure: Technical issue extracting from this site type

Confidence:
- high: Clear gap identified, safe to fix automatically
- medium: Likely gap, make change but notify admin
- low: Unclear, flag for admin review

Return ONLY this JSON:
{{
  "gap_type": "<one of the four types above>",
  "confidence": "<high, medium, or low>",
  "explanation": "<one paragraph plain English explanation>",
  "recommended_changes": ["<specific change 1>", "<specific change 2>"],
  "source_to_add": "<URL to add to watch list or null>",
  "keywords_to_add": ["<keyword 1>", "<keyword 2>"]
}}"""

    def _parse_ai_response(self, raw_text: str) -> dict:
        """
        Parses Claude's JSON analysis response.

        Args:
            raw_text: Raw text from Claude.

        Returns:
            Parsed analysis dictionary.
        """
        start = raw_text.find("{")
        end   = raw_text.rfind("}")
        if start == -1 or end == -1:
            return {
                "gap_type":            "unknown",
                "confidence":          "low",
                "explanation":         "Could not parse AI response.",
                "recommended_changes": [],
                "keywords_to_add":     [],
                "source_to_add":       None,
            }

        try:
            return json.loads(raw_text[start:end + 1])
        except json.JSONDecodeError:
            return {
                "gap_type":            "unknown",
                "confidence":          "low",
                "explanation":         "Could not parse AI response.",
                "recommended_changes": [],
                "keywords_to_add":     [],
                "source_to_add":       None,
            }

    def _merge_analysis(
        self,
        rule_analysis: dict,
        ai_analysis:   dict
    ) -> dict:
        """
        Merges rule-based and AI analysis into final result.

        If rule checks clearly identify the gap, boost confidence.
        If rule checks are inconclusive, defer to AI confidence.

        Args:
            rule_analysis: Results from rule-based checks.
            ai_analysis:   Results from AI analysis.

        Returns:
            Merged final analysis dictionary.
        """
        final = ai_analysis.copy()

        # If source is clearly not monitored boost to high confidence
        if (not rule_analysis["source_monitored"] and
                not rule_analysis["funder_in_watchlist"]):
            if final["confidence"] != "high":
                final["confidence"]  = "high"
                final["gap_type"]    = "source_not_monitored"

        # Ensure source_to_add is set if source not monitored
        if (final["gap_type"] == "source_not_monitored" and
                not final.get("source_to_add")):
            final["source_to_add"] = ai_analysis.get(
                "source_to_add",
                None
            )

        return final