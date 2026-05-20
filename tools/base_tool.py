"""
tools/base_tool.py
------------------
BaseTool — the abstract blueprint that every search tool must follow.

Every data source in the grant prospecting agent is a tool. Each tool
is a separate Python file in the /tools folder. Each tool must inherit
from BaseTool and implement the search() method.

This design means:
- The agent loop never cares which tool it is calling
- All tools return results in exactly the same format
- Adding a new grant source = writing one new file that inherits BaseTool
- If one tool fails, the others keep running

The GrantOpportunity model (defined below) is the universal result format.
Every tool must return a list of GrantOpportunity objects — nothing else.

Usage:
    # You never use BaseTool directly.
    # You use the tools that inherit from it:
    from tools.web_search import WebSearchTool
    from tools.grants_gov import GrantsGovTool

    tool    = WebSearchTool(profile)
    results = tool.search("permanent supportive housing grants Chicago")
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from agent.profile import OrgProfile


# ─────────────────────────────────────────────────────────────────────────────
# GrantOpportunity — the universal result format
#
# Every tool must return results as a list of GrantOpportunity objects.
# This is the single most important data model in the entire system.
# The scorer, formatter, exporter, and portal all read from this model.
#
# Think of it as the standard container that grant data gets poured into
# regardless of where it came from.
# ─────────────────────────────────────────────────────────────────────────────

class FundingType(str, Enum):
    """
    How the grant money can be used.
    Used to filter and tag results for the development team.
    """
    GENERAL_OPERATING   = "general_operating"
    PROJECT_SPECIFIC    = "project_specific"
    CAPITAL             = "capital"
    CAPACITY_BUILDING   = "capacity_building"
    RESEARCH            = "research"
    UNKNOWN             = "unknown"


class OpportunityStatus(str, Enum):
    """
    The current status of a grant opportunity.
    Only OPEN opportunities ever reach the results output.
    All others are filtered out by the eligibility filter.
    """
    OPEN        = "open"        # Currently accepting applications — the only one we want
    CLOSED      = "closed"      # Deadline has passed
    UPCOMING    = "upcoming"    # Not yet open but announced
    INVITE_ONLY = "invite_only" # No open application path
    UNKNOWN     = "unknown"     # Could not be determined


class GrantOpportunity(BaseModel):
    """
    The universal data model for a grant opportunity.

    Every search tool returns a list of these objects regardless of
    which source the data came from. This is what gets passed to the
    eligibility filter, then the scorer, then the formatter, then the
    exporter.

    The Who and The How — our non-negotiable output standard — are both
    captured in this model. No opportunity reaches the results output
    without both being populated.
    """

    # ── Unique identifier ─────────────────────────────────────────────────────
    opportunity_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier for this opportunity. Auto-generated."
    )

    # ── THE WHO ───────────────────────────────────────────────────────────────
    # The specific funding organization behind this specific open opportunity.

    funder_name: str = Field(
        ...,
        description="THE WHO — Full legal name of the funding organization. "
                    "e.g. 'Polk Bros. Foundation'"
    )
    program_name: str = Field(
        ...,
        description="THE WHO — Name of the specific grant program or initiative. "
                    "e.g. 'Community Impact Grant Program 2026'"
    )
    program_officer: Optional[str] = Field(
        None,
        description="THE WHO — Contact name or program officer if publicly available."
    )
    funder_website: Optional[str] = Field(
        None,
        description="THE WHO — URL of the funder's website or specific program page."
    )
    funder_type: Optional[str] = Field(
        None,
        description="THE WHO — Category of funder e.g. private_foundation, community_foundation."
    )

    # ── THE HOW ───────────────────────────────────────────────────────────────
    # Exact eligibility requirements and immediate application steps.

    application_deadline: Optional[date] = Field(
        None,
        description="THE HOW — Current application deadline as a specific date. "
                    "If this cannot be verified as current, the opportunity is excluded."
    )
    eligibility_requirements: Optional[str] = Field(
        None,
        description="THE HOW — Exact eligibility requirements as stated by the funder. "
                    "Copied or closely paraphrased from the funder's official materials."
    )
    award_min: Optional[int] = Field(
        None,
        description="THE HOW — Minimum award amount in USD."
    )
    award_max: Optional[int] = Field(
        None,
        description="THE HOW — Maximum award amount in USD."
    )
    application_url: Optional[str] = Field(
        None,
        description="THE HOW — Direct URL to the application portal or submission page."
    )
    application_method: Optional[str] = Field(
        None,
        description="THE HOW — How to apply: 'online_portal', 'email', 'mail', 'loi_first'."
    )
    required_documents: Optional[list[str]] = Field(
        None,
        description="THE HOW — List of documents required for the application."
    )
    disqualifying_factors: Optional[list[str]] = Field(
        None,
        description="THE HOW — Any restrictions that could disqualify this organization."
    )

    # ── Additional context ────────────────────────────────────────────────────

    description: Optional[str] = Field(
        None,
        description="Plain-language description of the grant opportunity."
    )
    funding_type: FundingType = Field(
        FundingType.UNKNOWN,
        description="How the grant money can be used."
    )
    status: OpportunityStatus = Field(
        OpportunityStatus.UNKNOWN,
        description="Current status of the opportunity. Must be OPEN to pass filtering."
    )
    geographic_focus: Optional[str] = Field(
        None,
        description="Geographic focus of the grant e.g. 'Chicago', 'Illinois', 'National'."
    )
    focus_areas: Optional[list[str]] = Field(
        None,
        description="Program areas or themes the funder is targeting with this grant."
    )
    source_name: str = Field(
        ...,
        description="Name of the tool or database that found this opportunity. "
                    "e.g. 'WebSearchTool', 'GrantsGovTool'. Used for deduplication and logging."
    )
    source_url: Optional[str] = Field(
        None,
        description="URL where this opportunity was found."
    )
    date_found: date = Field(
        default_factory=date.today,
        description="Date this opportunity was identified by the agent."
    )
    raw_data: Optional[dict] = Field(
        None,
        description="The raw data returned by the source before normalization. "
                    "Stored for debugging and the learning loop gap analyzer."
    )

    # ── Scoring fields ────────────────────────────────────────────────────────
    # These are populated by the scorer after the tool returns results.
    # They start empty — the tool never sets these.

    score_geographic:   Optional[float] = Field(None, description="Geographic alignment score 1-5")
    score_population:   Optional[float] = Field(None, description="Population served alignment score 1-5")
    score_budget:       Optional[float] = Field(None, description="Budget fit score 1-5")
    score_timeline:     Optional[float] = Field(None, description="Timeline feasibility score 1-5")
    score_composite:    Optional[float] = Field(None, description="Weighted composite score")
    score_final:        Optional[float] = Field(None, description="Final score after deadline multiplier")

    reason_geographic:  Optional[str]  = Field(None, description="Written explanation for geographic score")
    reason_population:  Optional[str]  = Field(None, description="Written explanation for population score")
    reason_budget:      Optional[str]  = Field(None, description="Written explanation for budget score")
    reason_timeline:    Optional[str]  = Field(None, description="Written explanation for timeline score")

    loi_draft:          Optional[str]  = Field(None, description="Draft LOI opening paragraph if generated")

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def days_until_deadline(self) -> Optional[int]:
        """
        Returns the number of days until the application deadline.
        Returns None if no deadline is set.
        Used by the scorer to apply the deadline proximity multiplier.
        """
        if self.application_deadline is None:
            return None
        delta = self.application_deadline - date.today()
        return delta.days

    @property
    def award_range_display(self) -> str:
        """
        Returns a human-readable award range string.
        Used in the results output and portal display.
        e.g. '$25,000 – $100,000' or 'Amount not specified'
        """
        if self.award_min and self.award_max:
            return f"${self.award_min:,} – ${self.award_max:,}"
        elif self.award_max:
            return f"Up to ${self.award_max:,}"
        elif self.award_min:
            return f"From ${self.award_min:,}"
        return "Amount not specified"

    @property
    def has_complete_who(self) -> bool:
        """
        Returns True if The Who is sufficiently complete.
        The funder name and program name are the minimum required.
        """
        return bool(self.funder_name and self.program_name)

    @property
    def has_complete_how(self) -> bool:
        """
        Returns True if The How is sufficiently complete.
        At minimum we need a deadline and either a URL or eligibility info.
        """
        return bool(
            self.application_deadline and
            (self.application_url or self.eligibility_requirements)
        )

    @property
    def is_actionable(self) -> bool:
        """
        Returns True if this opportunity meets the minimum standard
        for inclusion in results — The Who and The How both complete,
        status is OPEN, and deadline has not passed.
        """
        return (
            self.has_complete_who and
            self.has_complete_how and
            self.status == OpportunityStatus.OPEN and
            self.days_until_deadline is not None and
            self.days_until_deadline > 0
        )

    def display_summary(self) -> str:
        """
        Returns a one-line human-readable summary of the opportunity.
        Used in logs and terminal output.
        """
        deadline_str = (
            self.application_deadline.strftime("%B %d, %Y")
            if self.application_deadline else "No deadline found"
        )
        return (
            f"[{self.source_name}] {self.funder_name} — {self.program_name} | "
            f"Deadline: {deadline_str} | "
            f"Award: {self.award_range_display}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# BaseTool — the abstract blueprint
# ─────────────────────────────────────────────────────────────────────────────

class BaseTool(ABC):
    """
    Abstract base class that every search tool must inherit from.

    All tools in the /tools folder inherit from this class. This guarantees
    that every tool:
    - Accepts the same inputs (OrgProfile + search query)
    - Returns results in the same format (list of GrantOpportunity)
    - Handles errors the same way (logs and returns empty list)
    - Has a name and description the tool registry can read

    To build a new tool:
        1. Create a new file in /tools e.g. tools/my_new_source.py
        2. Import BaseTool and GrantOpportunity
        3. Create a class that inherits BaseTool
        4. Implement the search() method
        5. Register it in the tool registry (tools/__init__.py)
        That is all. The agent will automatically use it.

    Example:
        class MyNewSourceTool(BaseTool):
            name        = "MyNewSourceTool"
            description = "Searches MyNewSource.com for grant opportunities"

            def search(self, query: str) -> list[GrantOpportunity]:
                # Your search logic here
                results = []
                # ... fetch and parse data ...
                # ... build GrantOpportunity objects ...
                return results
    """

    # Every subclass must set these two class-level attributes.
    # The tool registry reads them to identify and describe each tool.
    name:        str  = "BaseTool"
    description: str  = "Abstract base tool — do not use directly"
    enabled:     bool = True   # Set to False to disable a tool without removing it

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the tool with the org profile.

        Args:
            profile: The loaded and validated OrgProfile for the current org.
                     Tools use the profile to filter and contextualize results.
        """
        self.profile = profile

    @abstractmethod
    def search(self, query: str) -> list[GrantOpportunity]:
        """
        Search for grant opportunities matching the given query.

        This method MUST be implemented by every tool that inherits BaseTool.
        If a subclass does not implement this, Python will raise an error
        when you try to create an instance of that class.

        Args:
            query: A search query string generated by the KeywordMapper.
                   e.g. "permanent supportive housing grants women Chicago"

        Returns:
            A list of GrantOpportunity objects. Return an empty list []
            if no results are found or if an error occurs — never raise
            an exception that would crash the agent loop.

        Important:
            - Always return a list — even if it is empty
            - Never let an exception propagate out of this method
            - Set source_name to self.name on every GrantOpportunity you return
            - Only return opportunities that appear to be currently open
        """
        pass

    def run(self, query: str) -> list[GrantOpportunity]:
        """
        Safely executes the search() method with error handling.

        The agent loop calls run() not search() directly. This wrapper
        ensures that if a tool crashes for any reason — network timeout,
        site structure change, API rate limit — the agent catches the
        error, logs it, and continues with other tools.

        This is the graceful failure guarantee: one broken tool never
        crashes the entire prospecting run.

        Args:
            query: Search query string from the KeywordMapper.

        Returns:
            List of GrantOpportunity objects, or empty list if tool failed.
        """
        if not self.enabled:
            return []

        try:
            results = self.search(query)

            # Validate that every result has the required fields
            # Filter out any incomplete results silently
            valid_results = []
            for r in results:
                if isinstance(r, GrantOpportunity) and r.funder_name and r.program_name:
                    valid_results.append(r)

            return valid_results

        except Exception as e:
            # Log the error but do not crash
            # The agent continues with all other tools
            print(f"[{self.name}] Error during search for '{query}': {type(e).__name__}: {e}")
            return []

    def get_info(self) -> dict:
        """
        Returns a dictionary describing this tool.
        Used by the tool registry to list available tools.

        Returns:
            Dictionary with tool name, description, and enabled status.
        """
        return {
            "name":        self.name,
            "description": self.description,
            "enabled":     self.enabled,
            "org":         self.profile.org_short_name,
        }