"""
agent/prompt_builder.py
-----------------------
PromptBuilder — builds the agent's system prompt dynamically
from the loaded OrgProfile.

The system prompt is the set of instructions Claude reads at the
start of every API call. It tells Claude:
- Which organization it is working for
- What programs and populations the org serves
- What geographic area to focus on
- What grant size range to look for
- What to exclude
- How to behave and what to return

Because the prompt is built from the profile, the same agent engine
works for any nonprofit. Swap the profile, the prompt changes
automatically. No hardcoded org details anywhere in the engine.

Usage:
    from agent.prompt_builder import PromptBuilder
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    builder = PromptBuilder(profile)

    system_prompt = builder.build()
    search_prompt = builder.build_search_prompt("housing grants women Chicago")
"""

from __future__ import annotations

from datetime import date

from agent.profile import OrgProfile, FunderType


class PromptBuilder:
    """
    Builds dynamic system prompts from an OrgProfile.

    All prompts are generated fresh from the profile each time.
    No org-specific content is hardcoded in this class.
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the prompt builder with an org profile.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        self.profile = profile
        self.today   = date.today().strftime("%B %d, %Y")

    def build(self) -> str:
        """
        Builds the main system prompt for the grant prospecting agent.

        This prompt is passed as the system parameter in every Claude
        API call made by the agent loop. It establishes the agent's
        identity, mission, and behavioral rules for the entire session.

        Returns:
            Complete system prompt string.
        """
        org = self.profile

        # Build the exclusion rules section dynamically from profile
        exclusion_rules = self._build_exclusion_rules()

        # Build the program areas description
        programs = self._build_programs_description()

        # Build the populations description
        populations = self._build_populations_description()

        return f"""You are an autonomous grant prospecting agent working exclusively on behalf of {org.org_name}.

TODAY'S DATE: {self.today}

YOUR MISSION:
Your sole function is identifying current, open, and actionable grant funding opportunities
for {org.org_name}. Every result you surface must be an opportunity the organization can
act on today.

ORGANIZATION YOU SERVE:
Name:             {org.org_name}
Mission:          {org.mission_statement}
Location:         {org.geography.city}, {org.geography.state}
Programs:         {programs}
Populations:      {populations}
Grant range:      ${org.budget.request_floor:,} to ${org.budget.request_ceiling:,}

YOUR GOVERNING RULE:
Before surfacing any result, ask: Can {org.org_short_name} apply for this funding right now?
If the answer is no — the deadline has passed, the cycle is closed, the program is
invitation-only, or the information cannot be verified as current — do not include it.

WHAT YOU MUST DELIVER FOR EVERY RESULT:

THE WHO:
- Full legal name of the funding organization
- Name of the specific grant program or initiative
- Contact name or program officer if publicly available

THE HOW:
- Current application deadline (verified, specific date)
- Exact eligibility requirements as stated by the funder
- Award range (minimum and maximum)
- Application method with link or address
- Required documents or materials
- Any restrictions that could disqualify {org.org_short_name}

{exclusion_rules}

HOW YOU SEARCH:
Cast a wide net and filter intelligently. Search broadly across:
- Foundation websites and funder portals
- Philanthropy publications and RFP announcements
- IRS Form 990 public data
- Grant aggregator platforms
- State and regional funding agency pages
- Press releases and news from foundations announcing new funding
- Application management platforms used by funders

OPERATING PRINCIPLES:
- Accuracy over volume. Ten verified actionable results beat fifty unverified ones.
- Current information only. If you cannot confirm an opportunity is open, exclude it.
- Transparency in scoring. Every rating must be explainable.
- Urgency awareness. Opportunities closing sooner rank higher.
- Broad discovery, smart filtering. Search wide. Filter rigorously."""

    def build_search_prompt(self, query: str) -> str:
        """
        Builds a focused search prompt for a specific query.

        Used by the web search tool when it needs to give Claude
        context about what it is searching for and why.

        Args:
            query: The search query string from the keyword mapper.

        Returns:
            Complete search prompt string.
        """
        org = self.profile

        return f"""You are searching for grant opportunities on behalf of {org.org_name}.

SEARCH QUERY: {query}

ORGANIZATION CONTEXT:
- Mission: {org.mission_statement[:200]}
- Location: {org.geography.city}, {org.geography.state}
- Grant range: ${org.budget.request_floor:,} to ${org.budget.request_ceiling:,}
- Programs: {self._build_programs_description()}

Find grant opportunities matching this query that {org.org_short_name} can apply for TODAY.
Only return opportunities that are currently open with a future deadline.
Return results as a JSON array."""

    def build_scoring_context(self) -> str:
        """
        Builds a concise context string for the scoring prompt.

        Used by the scorer to give Claude just enough org context
        to score accurately without overwhelming the prompt.

        Returns:
            Concise context string for scoring.
        """
        org = self.profile
        return (
            f"Organization: {org.org_name}\n"
            f"Location: {org.geography.city}, {org.geography.state}\n"
            f"Mission: {org.mission_statement[:150]}...\n"
            f"Programs: {self._build_programs_description()}\n"
            f"Populations: {self._build_populations_description()}\n"
            f"Grant range: ${org.budget.request_floor:,} – ${org.budget.request_ceiling:,}"
        )

    def build_loi_prompt(self, opportunity_summary: str) -> str:
        """
        Builds the prompt for generating a letter of inquiry draft.

        Used by the LOI drafter (Phase 7) to generate tailored
        opening paragraphs for top-ranked grant opportunities.

        Args:
            opportunity_summary: Plain-text summary of the grant opportunity.

        Returns:
            Complete LOI draft prompt string.
        """
        org = self.profile

        return f"""Write a professional opening paragraph for a letter of inquiry
from {org.org_name} to the funder described below.

ORGANIZATION:
{org.org_name} — {org.mission_statement}
Location: {org.geography.city}, {org.geography.state}
Programs: {self._build_programs_description()}
Populations served: {self._build_populations_description()}

GRANT OPPORTUNITY:
{opportunity_summary}

Write one paragraph (4-6 sentences) that:
1. Introduces {org.org_short_name} and its mission concisely
2. States the specific amount being requested
3. Names the specific program the funding would support
4. Connects the organization's work directly to the funder's stated priorities
5. Ends with a clear statement of interest in the funding opportunity

Tone: Professional, confident, and specific. Avoid generic nonprofit language.
Do not use phrases like 'we are pleased to submit' or 'we are writing to request'.
Start with the organization's impact or mission."""

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_exclusion_rules(self) -> str:
        """
        Builds the exclusion rules section of the system prompt
        based on the org profile's current settings.

        Returns:
            Formatted exclusion rules string.
        """
        rules = []

        if self.profile.settings.exclude_federal:
            rules.append(
                "- EXCLUDE all federal government funding opportunities "
                "(HUD, SAMHSA, DOL, DOJ, and all other federal agencies)"
            )

        if self.profile.settings.exclude_state:
            rules.append(
                f"- EXCLUDE all {self.profile.geography.state} state "
                f"government funding opportunities"
            )

        if self.profile.funder_exclusions:
            excluded = ", ".join(self.profile.funder_exclusions)
            rules.append(f"- EXCLUDE opportunities from these specific funders: {excluded}")

        deadline_floor = self.profile.settings.deadline_floor_days
        rules.append(
            f"- EXCLUDE opportunities with deadlines fewer than "
            f"{deadline_floor} days away — insufficient preparation time"
        )

        rules.append("- EXCLUDE opportunities with passed deadlines")
        rules.append("- EXCLUDE invitation-only programs with no open application path")
        rules.append(
            "- EXCLUDE any opportunity where current deadline and "
            "eligibility cannot be verified"
        )

        if rules:
            return "EXCLUSION RULES — never include these in results:\n" + "\n".join(rules)
        return ""

    def _build_programs_description(self) -> str:
        """
        Builds a readable comma-separated list of program areas.

        Converts internal enum values to human-readable phrases.
        e.g. 'housing_permanent' becomes 'permanent housing'

        Returns:
            Comma-separated program areas string.
        """
        return ", ".join(
            p.value.replace("_", " ")
            for p in self.profile.program_areas
        )

    def _build_populations_description(self) -> str:
        """
        Builds a readable comma-separated list of populations served.

        Returns:
            Comma-separated populations string.
        """
        return ", ".join(
            p.value.replace("_", " ")
            for p in self.profile.populations_served
        )