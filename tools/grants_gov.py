"""
tools/grants_gov.py
-------------------
GrantsGovTool — searches Grants.gov for open federal grant opportunities.

Grants.gov is the official US federal grant database. It lists every
open federal grant opportunity across all agencies. This tool queries
the Grants.gov REST API which is:
- Completely free
- No API key required
- Structured data with deadlines, award amounts, and eligibility
- Updated daily with new opportunities

Important: For organizations with exclude_federal=True in their profile
settings, results from this tool will be filtered out by the eligibility
filter before reaching the results output. The tool always runs — the
profile settings control what appears in results.

Usage:
    from tools.grants_gov import GrantsGovTool
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    tool    = GrantsGovTool(profile)
    results = tool.run("transitional housing women")
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Optional

import requests

from agent.profile import OrgProfile
from tools.base_tool import BaseTool, GrantOpportunity, OpportunityStatus, FundingType


# Grants.gov v1 API — free, no authentication required
GRANTS_GOV_BASE_URL = "https://apply07.grants.gov/grantsws/rest/opportunities/search"


class GrantsGovTool(BaseTool):
    """
    Searches Grants.gov for open federal grant opportunities.

    Queries the Grants.gov v1 REST API and returns results as
    GrantOpportunity objects. Results are tagged with
    funder_type = government_federal so the eligibility filter
    can identify and handle them based on the org's settings.

    For orgs with exclude_federal=True, these results are filtered
    out before reaching the output. For orgs that want federal
    funding, they appear in the ranked results like any other source.
    """

    name        = "GrantsGovTool"
    description = "Searches Grants.gov federal database for open grant opportunities"
    enabled     = True

    # Delay between requests
    REQUEST_DELAY_SECONDS = 0.5

    # Number of results to request per search
    MAX_RESULTS = 20

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the GrantsGovTool.

        Args:
            profile: Loaded and validated OrgProfile.
        """
        super().__init__(profile)
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent":   "GrantProspectorAgent/1.0 (nonprofit grant research)"
        })

    def search(self, query: str) -> list[GrantOpportunity]:
        """
        Search Grants.gov for open opportunities matching the query.

        Args:
            query: Search query string from the KeywordMapper.
                   e.g. "transitional housing women Illinois"

        Returns:
            List of GrantOpportunity objects for open federal grants.
        """
        time.sleep(self.REQUEST_DELAY_SECONDS)
        return self._search_v1(query)

    def _search_v1(self, query: str) -> list[GrantOpportunity]:
        """
        Search using the Grants.gov v1 API.

        Args:
            query: Search query string.

        Returns:
            List of GrantOpportunity objects or empty list if fails.
        """
        payload = {
            "keyword":        query,
            "oppStatuses":    "posted",
            "rows":           self.MAX_RESULTS,
            "startRecordNum": 0,
        }

        try:
            response = self.session.post(
                GRANTS_GOV_BASE_URL,
                json=payload,
                timeout=20
            )

            if response.status_code != 200:
                print(f"[{self.name}] API returned status {response.status_code}")
                return []

            data     = response.json()
            opp_list = data.get("oppHits", [])

            print(f"[{self.name}] Raw results from Grants.gov: {len(opp_list)}")

            return [
                opp for item in opp_list
                if (opp := self._parse_v1_opportunity(item)) is not None
            ]

        except requests.exceptions.Timeout:
            print(f"[{self.name}] Request timed out for query: '{query}'")
            return []
        except requests.exceptions.RequestException as e:
            print(f"[{self.name}] Request error for query '{query}': {e}")
            return []
        except Exception as e:
            print(f"[{self.name}] Unexpected error for query '{query}': {e}")
            return []

    def _parse_v1_opportunity(self, item: dict) -> Optional[GrantOpportunity]:
        """
        Parses a v1 API result into a GrantOpportunity object.

        Field names confirmed from live API response:
        id, number, title, agencyCode, agency, openDate,
        closeDate, oppStatus, docType, cfdaList

        Args:
            item: Raw opportunity dictionary from the v1 API.

        Returns:
            GrantOpportunity object or None if required fields missing.
        """
        title      = item.get("title", "").strip()
        agency     = item.get("agency", "").strip()
        close_date = item.get("closeDate", "")
        opp_id     = item.get("id", "")
        cfda       = item.get("cfdaList", [])
        opp_number = item.get("number", "")

        # Skip if missing required fields
        if not title or not agency:
            return None

        # Parse deadline
        deadline = self._parse_date(close_date)

        # Skip if deadline has already passed
        if deadline and deadline < date.today():
            return None

        # Build application URL
        app_url = (
            f"https://www.grants.gov/search-results-detail/{opp_id}"
            if opp_id else "https://www.grants.gov"
        )

        # Build description
        description = f"Federal grant opportunity from {agency}."
        if cfda:
            description += f" CFDA: {', '.join(str(c) for c in cfda[:3])}."
        if opp_number:
            description += f" Opportunity number: {opp_number}."

        return GrantOpportunity(
            funder_name          = agency,
            program_name         = title,
            application_deadline = deadline,
            award_min            = None,
            award_max            = None,
            application_url      = app_url,
            application_method   = "online_portal",
            description          = description,
            funder_type          = "government_federal",
            funding_type         = FundingType.PROJECT_SPECIFIC,
            status               = OpportunityStatus.OPEN,
            source_name          = self.name,
            source_url           = app_url,
            raw_data             = item,
        )

    def _parse_date(self, date_str: Optional[str]) -> Optional[date]:
        """
        Parses a date string into a date object.
        Handles multiple date formats used by Grants.gov.

        Args:
            date_str: Date string in various formats.

        Returns:
            date object or None if parsing fails.
        """
        if not date_str:
            return None

        formats = [
            "%m/%d/%Y",
            "%Y-%m-%d",
            "%Y%m%d",
            "%m-%d-%Y",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(str(date_str).strip(), fmt).date()
            except (ValueError, TypeError):
                continue

        return None

    def _parse_int(self, value) -> Optional[int]:
        """
        Safely parses a value to integer.

        Args:
            value: Value to parse.

        Returns:
            Integer or None if parsing fails.
        """
        if value is None:
            return None
        try:
            return int(float(str(value).replace(",", "").replace("$", "")))
        except (ValueError, TypeError):
            return None