"""
tools/web_search.py
-------------------
WebSearchTool — searches the web for current, open grant opportunities.

This tool uses the Claude API with built-in web search to intelligently
find grant opportunities across the entire web — not just structured
databases. It covers foundation websites, philanthropy publications,
funder press releases, and anywhere else grants are announced publicly.

This is the primary tool for finding private foundation grants and the
fallback for any source that does not have a structured API.

The tool instructs Claude to:
1. Search the web for the given query
2. Read and evaluate the results
3. Extract only currently open opportunities
4. Return structured data we can build GrantOpportunity objects from

Usage:
    from tools.web_search import WebSearchTool
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    tool    = WebSearchTool(profile)
    results = tool.run("permanent supportive housing grants women Chicago 2026")
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from typing import Optional

import anthropic
from dotenv import load_dotenv

from agent.profile import OrgProfile
from tools.base_tool import BaseTool, GrantOpportunity, OpportunityStatus, FundingType

# Load environment variables from .env file
load_dotenv()


class WebSearchTool(BaseTool):
    """
    Searches the web for current, open grant opportunities using Claude AI.

    Inherits from BaseTool — returns results as list[GrantOpportunity].

    The tool sends each search query to Claude along with a detailed
    system prompt instructing it to find only open, actionable grant
    opportunities and return the results in a structured JSON format
    we can parse into GrantOpportunity objects.
    """

    name        = "WebSearchTool"
    description = "Searches the web for current open grant opportunities using Claude AI with web search"
    enabled     = True

    # How many results to request per search query
    # Kept low to maintain quality over quantity
    MAX_RESULTS_PER_QUERY = 5

    # Delay between API calls to avoid rate limiting
    # 1 second between calls is a safe default
    REQUEST_DELAY_SECONDS = 10.0

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the web search tool.

        Args:
            profile: Loaded and validated OrgProfile for the current org.
        """
        super().__init__(profile)

        # Initialize the Anthropic client
        # It reads the ANTHROPIC_API_KEY from the .env file automatically
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not found in environment. "
                "Check your .env file."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

    def search(self, query: str) -> list[GrantOpportunity]:
        """
        Search the web for grant opportunities matching the given query.

        Sends the query to Claude with web search enabled. Claude searches
        the web, reads the results, and extracts structured grant data.
        We parse that data into GrantOpportunity objects.

        Args:
            query: Search query string from the KeywordMapper.
                   e.g. "permanent supportive housing grants women Chicago 2026"

        Returns:
            List of GrantOpportunity objects for open opportunities found.
            Returns empty list if no results found or if search fails.
        """
        # Add a small delay to avoid hitting API rate limits
        time.sleep(self.REQUEST_DELAY_SECONDS)

        # Build the prompt that tells Claude exactly what to search for
        # and how to return the results
        search_prompt = self._build_search_prompt(query)

        try:
            # Call the Claude API with web search tool enabled
            response = self.client.messages.create(
               model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search"
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": search_prompt
                    }
                ]
            )

            # Extract the text response from Claude
            raw_text = self._extract_text_from_response(response)
            if not raw_text:
                return []

            # Parse Claude's response into GrantOpportunity objects
            opportunities = self._parse_response(raw_text, query)
            return opportunities

        except anthropic.RateLimitError:
            # We hit the API rate limit — wait longer and signal caller
            print(f"[{self.name}] Rate limit hit. Waiting 60 seconds...")
            time.sleep(60)
            return []

        except anthropic.APIError as e:
            print(f"[{self.name}] API error for query '{query}': {e}")
            return []

    def _build_search_prompt(self, query: str) -> str:
        """
        Builds the prompt sent to Claude for each search query.

        The prompt gives Claude all the context it needs about the
        organization and instructs it to return structured JSON data
        we can parse into GrantOpportunity objects.

        Args:
            query: The search query string.

        Returns:
            Complete prompt string ready to send to the Claude API.
        """
        today = date.today().strftime("%B %d, %Y")
        org   = self.profile

        return f"""You are a grant research assistant helping {org.org_name} find funding.

Today's date is {today}.

ORGANIZATION CONTEXT:
- Name: {org.org_name}
- Mission: {org.mission_statement}
- Location: {org.geography.city}, {org.geography.state}
- Programs: {', '.join(p.value.replace('_', ' ') for p in org.program_areas)}
- Populations served: {', '.join(p.value.replace('_', ' ') for p in org.populations_served)}
- Typical grant request: ${org.budget.request_floor:,} to ${org.budget.request_ceiling:,}

SEARCH QUERY: {query}

YOUR TASK:
Search the web for this query and find grant opportunities that {org.org_name} can apply for RIGHT NOW.

STRICT RULES — only include opportunities that meet ALL of these:
1. The grant cycle is currently OPEN and accepting applications today
2. The application deadline is in the future (after {today})
3. The deadline is specific and verifiable — not vague like "rolling" unless confirmed
4. The organization ({org.org_name}) appears to meet the eligibility requirements
5. The award amount falls within ${org.budget.request_floor:,} to ${org.budget.request_ceiling:,}

DO NOT include:
- Opportunities with past deadlines
- Closed grant cycles
- Invitation-only programs
- Opportunities where eligibility is clearly not met
- Results that are just funder profile pages with no open opportunity

Return your findings as a JSON array. Each opportunity must follow this exact format:
{{
  "funder_name": "Full legal name of the funding organization",
  "program_name": "Specific name of this grant program",
  "program_officer": "Contact name if found, or null",
  "funder_website": "Foundation website URL",
  "application_url": "Direct link to apply or learn more",
  "application_deadline": "YYYY-MM-DD format, or null if not found",
  "award_min": minimum award amount as integer or null,
  "award_max": maximum award amount as integer or null,
  "eligibility_requirements": "Exact eligibility requirements as stated",
  "application_method": "online_portal or email or mail or loi_first or unknown",
  "required_documents": ["list", "of", "required", "documents"] or null,
  "disqualifying_factors": ["factors that could disqualify this org"] or null,
  "description": "Brief description of what this grant funds",
  "geographic_focus": "Geographic area this grant serves",
  "focus_areas": ["list", "of", "program", "areas", "funded"],
  "funding_type": "general_operating or project_specific or capacity_building or unknown",
  "source_url": "URL where you found this opportunity"
}}

If you find no qualifying open opportunities, return an empty array: []

Return ONLY the JSON array. No introduction, no explanation, no markdown formatting.
Just the raw JSON starting with [ and ending with ]"""

    def _extract_text_from_response(self, response) -> str:
        """
        Extracts the text content from Claude's API response.

        Claude's response can contain multiple content blocks including
        tool use blocks (web search calls) and text blocks (the answer).
        We want only the final text block containing the JSON results.

        Args:
            response: The raw response object from the Anthropic API.

        Returns:
            The text content as a string, or empty string if not found.
        """
        text_parts = []
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                text_parts.append(block.text)
        return "\n".join(text_parts).strip()

    def _parse_response(self, raw_text: str, query: str) -> list[GrantOpportunity]:
        """
        Parses Claude's JSON response into GrantOpportunity objects.

        Claude is instructed to return a JSON array. This method extracts
        that JSON, parses it, and builds GrantOpportunity objects from
        each result that passes basic validation.

        Args:
            raw_text: The text response from Claude.
            query:    The original search query (used for logging).

        Returns:
            List of valid GrantOpportunity objects.
        """
        opportunities = []

        # Find the JSON array in the response
        # Sometimes Claude adds a small amount of text before or after
        # the JSON despite being told not to — we handle that here
        json_text = self._extract_json(raw_text)
        if not json_text:
            return []

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as e:
            print(f"[{self.name}] Could not parse JSON response for '{query}': {e}")
            return []

        if not isinstance(data, list):
            return []

        for item in data:
            try:
                opp = self._build_opportunity(item)
                if opp:
                    opportunities.append(opp)
            except Exception as e:
                # Skip malformed results silently
                print(f"[{self.name}] Skipping malformed result: {e}")
                continue

        return opportunities

    def _extract_json(self, text: str) -> Optional[str]:
        """
        Extracts a JSON array from a text string.

        Finds the first [ and last ] in the text and returns everything
        between them. This handles cases where Claude adds extra text
        around the JSON despite being instructed not to.

        Args:
            text: Raw text that should contain a JSON array.

        Returns:
            JSON string if found, None otherwise.
        """
        start = text.find("[")
        end   = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        return text[start:end + 1]

    def _build_opportunity(self, item: dict) -> Optional[GrantOpportunity]:
        """
        Builds a GrantOpportunity object from a parsed JSON dictionary.

        Validates required fields, parses dates, and maps the raw data
        to the GrantOpportunity schema.

        Args:
            item: Dictionary parsed from Claude's JSON response.

        Returns:
            A GrantOpportunity object if valid, None if required fields
            are missing or the data is clearly invalid.
        """
        # Required fields — skip if missing
        funder_name  = item.get("funder_name", "").strip()
        program_name = item.get("program_name", "").strip()
        if not funder_name or not program_name:
            return None

        # Parse the deadline date
        deadline = self._parse_date(item.get("application_deadline"))

        # Skip opportunities where deadline has already passed
        if deadline and deadline < date.today():
            return None

        # Parse award amounts — ensure they are integers
        award_min = self._parse_int(item.get("award_min"))
        award_max = self._parse_int(item.get("award_max"))

        # Map funding type string to enum
        funding_type_str = item.get("funding_type", "unknown")
        try:
            funding_type = FundingType(funding_type_str)
        except ValueError:
            funding_type = FundingType.UNKNOWN

        return GrantOpportunity(
            funder_name              = funder_name,
            program_name             = program_name,
            program_officer          = item.get("program_officer"),
            funder_website           = item.get("funder_website"),
            application_url          = item.get("application_url"),
            application_deadline     = deadline,
            eligibility_requirements = item.get("eligibility_requirements"),
            award_min                = award_min,
            award_max                = award_max,
            application_method       = item.get("application_method"),
            required_documents       = item.get("required_documents"),
            disqualifying_factors    = item.get("disqualifying_factors"),
            description              = item.get("description"),
            geographic_focus         = item.get("geographic_focus"),
            focus_areas              = item.get("focus_areas"),
            funding_type             = funding_type,
            status                   = OpportunityStatus.OPEN,
            source_name              = self.name,
            source_url               = item.get("source_url"),
            raw_data                 = item,
        )

    def _parse_date(self, date_str: Optional[str]) -> Optional[date]:
        """
        Parses a date string in YYYY-MM-DD format into a date object.

        Args:
            date_str: Date string e.g. "2026-08-15" or None.

        Returns:
            date object or None if parsing fails.
        """
        if not date_str:
            return None
        try:
            return datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    def _parse_int(self, value) -> Optional[int]:
        """
        Safely parses a value to integer.

        Args:
            value: Value to parse — could be int, float, string, or None.

        Returns:
            Integer or None if parsing fails.
        """
        if value is None:
            return None
        try:
            return int(float(str(value).replace(",", "").replace("$", "")))
        except (ValueError, TypeError):
            return None