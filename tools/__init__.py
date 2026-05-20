"""
tools/__init__.py
-----------------
Tool Registry — discovers, registers, and runs all search tools.

This is the single entry point the agent loop uses to run searches.
It manages all tool instances, runs them against queries, merges
results, and removes duplicates.

How the plugin architecture works:
- Every tool in this file is registered in REGISTERED_TOOLS
- The agent loop calls ToolRegistry.run_all_tools(query)
- Results from all tools are merged and deduplicated automatically
- To add a new tool: write the tool file, add it to REGISTERED_TOOLS
- Nothing else in the codebase needs to change

Usage:
    from tools import ToolRegistry
    from agent.profile import OrgProfile

    profile  = OrgProfile.from_json("profiles/deborah_place.json")
    registry = ToolRegistry(profile)

    # Run all active tools against a single query
    results  = registry.run_all_tools("housing grants women Chicago")

    # Run all tools against multiple queries
    results  = registry.run_all_queries(["query 1", "query 2"])

    # Get info about registered tools
    registry.print_status()
"""

from __future__ import annotations

import hashlib
from typing import Type

from agent.profile import OrgProfile
from tools.base_tool import BaseTool, GrantOpportunity
from tools.web_search import WebSearchTool
from tools.form_990 import Form990Tool
from tools.grants_gov import GrantsGovTool


# ─────────────────────────────────────────────────────────────────────────────
# REGISTERED_TOOLS
#
# This is the only place you need to edit to add a new tool.
# Add the class to this list and it will automatically be picked up
# by the registry. The agent loop never needs to change.
#
# To disable a tool without removing it, set enabled = False
# in the tool's class definition.
# ─────────────────────────────────────────────────────────────────────────────

REGISTERED_TOOLS: list[Type[BaseTool]] = [
    WebSearchTool,   # Searches web for open grant opportunities
    Form990Tool,     # Mines IRS 990 data for aligned foundations
    GrantsGovTool,   # Searches Grants.gov federal database
    # CandidTool,    # Phase 7 — Candid Foundation Directory
    # InstrumentlTool, # Phase 7 — Instrumentl platform
]


class ToolRegistry:
    """
    Manages all registered search tools and coordinates their execution.

    The registry:
    - Instantiates each registered tool with the org profile
    - Runs tools against search queries
    - Merges results from all tools into one list
    - Deduplicates results so the same opportunity is never shown twice
    - Tracks which tools are active and which are disabled
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the registry with the org profile.

        Instantiates all registered tools with the profile.
        Disabled tools are instantiated but skipped during search.

        Args:
            profile: Loaded and validated OrgProfile for the current org.
        """
        self.profile = profile
        self.tools: list[BaseTool] = []

        # Instantiate every registered tool
        for tool_class in REGISTERED_TOOLS:
            try:
                tool = tool_class(profile)
                self.tools.append(tool)
            except Exception as e:
                # If a tool fails to initialize, log it and continue
                # One broken tool should never prevent others from working
                print(f"[ToolRegistry] Failed to initialize {tool_class.name}: {e}")

    def run_all_tools(self, query: str) -> list[GrantOpportunity]:
        """
        Runs all active tools against a single search query.

        Each tool is run independently. Results are merged into one
        list and deduplicated before returning.

        Args:
            query: Search query string from the KeywordMapper.

        Returns:
            Deduplicated list of GrantOpportunity objects from all tools.
        """
        all_results: list[GrantOpportunity] = []

        for tool in self.tools:
            if not tool.enabled:
                continue

            print(f"[ToolRegistry] Running {tool.name} for: '{query[:50]}...'")

            # run() handles errors gracefully — never raises exceptions
            results = tool.run(query)
            print(f"[ToolRegistry] {tool.name} returned {len(results)} results")

            all_results.extend(results)

        # Deduplicate before returning
        deduplicated = self._deduplicate(all_results)
        print(f"[ToolRegistry] Total after deduplication: {len(deduplicated)}")

        return deduplicated

    def run_all_queries(
        self,
        queries: list[str],
        max_queries: int = 20
    ) -> list[GrantOpportunity]:
        """
        Runs all active tools against multiple search queries.

        Used by the monitoring cycle which runs many queries from
        the keyword mapper in a single session.

        Args:
            queries:     List of search query strings.
            max_queries: Maximum number of queries to run in one session.
                         Prevents runaway API costs. Default 20.

        Returns:
            Deduplicated list of all GrantOpportunity objects found
            across all queries and all tools.
        """
        all_results: list[GrantOpportunity] = []

        # Limit queries to prevent runaway costs
        limited_queries = queries[:max_queries]

        print(f"[ToolRegistry] Running {len(limited_queries)} queries across {len(self.get_active_tools())} tools")

        for i, query in enumerate(limited_queries, 1):
            print(f"[ToolRegistry] Query {i}/{len(limited_queries)}: '{query[:60]}'")
            results = self.run_all_tools(query)
            all_results.extend(results)

        # Final deduplication across all queries
        final = self._deduplicate(all_results)
        print(f"[ToolRegistry] Final total across all queries: {len(final)} unique opportunities")

        return final

    def get_active_tools(self) -> list[BaseTool]:
        """
        Returns only the tools that are currently enabled.

        Returns:
            List of enabled BaseTool instances.
        """
        return [t for t in self.tools if t.enabled]

    def get_tool(self, name: str) -> BaseTool | None:
        """
        Returns a specific tool by name.

        Useful when a cycle wants to run one specific tool
        rather than all tools at once.

        Args:
            name: Tool name e.g. "WebSearchTool"

        Returns:
            The tool instance or None if not found.
        """
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def print_status(self) -> None:
        """
        Prints a human-readable status summary of all registered tools.
        Used in logs and the agent startup sequence.
        """
        print(f"\n{'='*50}")
        print(f"Tool Registry — {self.profile.org_short_name}")
        print(f"{'='*50}")
        print(f"Registered tools: {len(self.tools)}")
        print(f"Active tools:     {len(self.get_active_tools())}")
        print()
        for tool in self.tools:
            status = "✓ ACTIVE" if tool.enabled else "✗ DISABLED"
            print(f"  {status} — {tool.name}")
            print(f"           {tool.description}")
        print(f"{'='*50}\n")

    # ── Private methods ───────────────────────────────────────────────────────

    def _deduplicate(
        self,
        opportunities: list[GrantOpportunity]
    ) -> list[GrantOpportunity]:
        """
        Removes duplicate opportunities from the results list.

        Deduplication is based on a fingerprint built from the
        funder name and program name. If two tools find the same
        grant, only the first one found is kept.

        This handles the common case where the same grant appears
        in multiple sources — e.g. a foundation RFP that appears
        in both the web search results and a grant database.

        Args:
            opportunities: List of GrantOpportunity objects to deduplicate.

        Returns:
            Deduplicated list preserving the original order.
        """
        seen_fingerprints: set[str] = set()
        unique: list[GrantOpportunity] = []

        for opp in opportunities:
            # Build a fingerprint from funder name + program name
            # Normalized to lowercase with whitespace removed
            fingerprint_raw = (
                opp.funder_name.lower().strip() +
                "|" +
                opp.program_name.lower().strip()
            )

            # Hash it for consistent comparison
            fingerprint = hashlib.md5(
                fingerprint_raw.encode()
            ).hexdigest()

            if fingerprint not in seen_fingerprints:
                seen_fingerprints.add(fingerprint)
                unique.append(opp)

        duplicates_removed = len(opportunities) - len(unique)
        if duplicates_removed > 0:
            print(f"[ToolRegistry] Removed {duplicates_removed} duplicate results")

        return unique