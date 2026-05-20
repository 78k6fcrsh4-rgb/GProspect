"""
tools/form_990.py
-----------------
Form990Tool — mines IRS Form 990 public data to identify foundations
whose giving patterns align with the organization's mission and programs.

This tool uses the ProPublica Nonprofit Explorer API which is:
- Completely free
- No API key required
- No rate limits (be respectful with requests)
- Updated regularly with new 990 filings

What this tool does:
1. Searches for nonprofits and foundations by keyword and location
2. Retrieves their 990 filing data to see who they have funded
3. Identifies foundations whose giving history aligns with the org profile
4. Returns them as GrantOpportunity objects flagged for relationship building

This is the primary source for the Discovery Cycle and Relationship
Mapping Cycle — it finds funders that no grant database lists because
they have not published an open RFP, but their giving history shows
they would be a strong prospect.

Usage:
    from tools.form_990 import Form990Tool
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    tool    = Form990Tool(profile)
    results = tool.run("women housing Chicago foundation")
"""

from __future__ import annotations

import time
from datetime import date
from typing import Optional

import requests

from agent.profile import OrgProfile
from tools.base_tool import BaseTool, GrantOpportunity, OpportunityStatus, FundingType


# ProPublica Nonprofit Explorer API base URL
# Completely free, no authentication required
PROPUBLICA_BASE_URL = "https://projects.propublica.org/nonprofits/api/v2"


class Form990Tool(BaseTool):
    """
    Mines IRS Form 990 public data via the ProPublica Nonprofit Explorer API.

    This tool searches for foundations and nonprofits whose giving history
    aligns with the org profile. Results are returned as GrantOpportunity
    objects with status UPCOMING — meaning they are not currently open RFPs
    but are strong prospects for relationship building and future applications.

    The Discovery Cycle uses this tool to expand the agent's watch list.
    The Relationship Mapping Cycle uses it to build the funder intelligence map.
    """

    name        = "Form990Tool"
    description = "Mines IRS 990 public data via ProPublica API to find aligned foundations"
    enabled     = True

    # Delay between API requests — be respectful to the free API
    REQUEST_DELAY_SECONDS = 0.5

    # Number of search results to retrieve per query
    MAX_RESULTS = 10

    # Minimum total giving amount to consider a foundation relevant
    # Filters out very small foundations unlikely to fund at our scale
    MIN_TOTAL_GIVING = 100000

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the Form990Tool.

        Args:
            profile: Loaded and validated OrgProfile for the current org.
        """
        super().__init__(profile)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "GrantProspectorAgent/1.0 (nonprofit grant research tool)"
        })

    def search(self, query: str) -> list[GrantOpportunity]:
        """
        Search ProPublica's nonprofit database for foundations aligned
        with the org profile using the given query.

        Args:
            query: Search query string from the KeywordMapper or discovery cycle.
                   e.g. "women housing Chicago foundation"

        Returns:
            List of GrantOpportunity objects representing aligned foundations.
            These are flagged as relationship-building prospects, not open RFPs.
        """
        time.sleep(self.REQUEST_DELAY_SECONDS)

        # Search ProPublica for nonprofits matching the query
        organizations = self._search_organizations(query)
        if not organizations:
            return []

        opportunities = []

        for org in organizations[:self.MAX_RESULTS]:
            try:
                # Get detailed 990 data for this organization
                details = self._get_org_details(org.get("ein", ""))
                if not details:
                    continue

                # Evaluate whether this org is a relevant funder
                opportunity = self._evaluate_funder(org, details)
                if opportunity:
                    opportunities.append(opportunity)

                # Small delay between detail requests
                time.sleep(self.REQUEST_DELAY_SECONDS)

            except Exception as e:
                print(f"[{self.name}] Error processing org {org.get('name', 'unknown')}: {e}")
                continue

        return opportunities

    def search_by_ntee(self, ntee_code: str) -> list[GrantOpportunity]:
        """
        Search for foundations by NTEE code.

        NTEE codes classify nonprofits by their mission area.
        This is useful for finding foundations that specifically fund
        the types of work the org does.

        Common foundation NTEE codes:
        - T20: Private Foundations
        - T21: Corporate Foundations
        - T22: Private Operating Foundations
        - T30: Public Foundations
        - T31: Community Foundations

        Args:
            ntee_code: NTEE code string e.g. "T20" for private foundations

        Returns:
            List of GrantOpportunity objects for matching foundations.
        """
        time.sleep(self.REQUEST_DELAY_SECONDS)

        url    = f"{PROPUBLICA_BASE_URL}/organizations.json"
        params = {
            "ntee_code": ntee_code,
            "state":     self.profile.geography.state,
            "per_page":  self.MAX_RESULTS,
        }

        try:
            response = self.session.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            organizations = data.get("organizations", [])

            opportunities = []
            for org in organizations:
                details = self._get_org_details(org.get("ein", ""))
                if details:
                    opp = self._evaluate_funder(org, details)
                    if opp:
                        opportunities.append(opp)
                    time.sleep(self.REQUEST_DELAY_SECONDS)

            return opportunities

        except Exception as e:
            print(f"[{self.name}] Error searching by NTEE {ntee_code}: {e}")
            return []

    def get_funder_history(self, ein: str) -> Optional[dict]:
        """
        Retrieves the full giving history for a specific foundation by EIN.

        Used by the Relationship Mapping Cycle to build the funder
        intelligence map and identify co-funding relationships.

        Args:
            ein: The foundation's IRS Employer Identification Number
                 e.g. "36-3786883"

        Returns:
            Dictionary containing the foundation's profile and giving
            history, or None if not found.
        """
        # Clean the EIN — remove dashes for the API
        ein_clean = ein.replace("-", "")
        return self._get_org_details(ein_clean)

    # ── Private methods ───────────────────────────────────────────────────────

    def _search_organizations(self, query: str) -> list[dict]:
        """
        Searches ProPublica's nonprofit database for organizations
        matching the given query string.

        Args:
            query: Search query string.

        Returns:
            List of organization dictionaries from the API.
        """
        url    = f"{PROPUBLICA_BASE_URL}/organizations.json"
        params = {
            "q":        query,
            "state":    self.profile.geography.state,
            "per_page": self.MAX_RESULTS,
        }

        try:
            response = self.session.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data.get("organizations", [])

        except requests.exceptions.Timeout:
            print(f"[{self.name}] Request timed out for query: '{query}'")
            return []
        except requests.exceptions.RequestException as e:
            print(f"[{self.name}] Request error for query '{query}': {e}")
            return []
        except Exception as e:
            print(f"[{self.name}] Unexpected error for query '{query}': {e}")
            return []

    def _get_org_details(self, ein: str) -> Optional[dict]:
        """
        Retrieves detailed 990 filing data for a specific organization.

        Args:
            ein: Organization's EIN number (with or without dashes).

        Returns:
            Dictionary with organization details and filing history,
            or None if not found or request fails.
        """
        if not ein:
            return None

        # Clean the EIN
        ein_clean = ein.replace("-", "").strip()
        if not ein_clean:
            return None

        url = f"{PROPUBLICA_BASE_URL}/organizations/{ein_clean}.json"

        try:
            response = self.session.get(url, timeout=15)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout:
            return None
        except requests.exceptions.RequestException:
            return None
        except Exception:
            return None

    def _evaluate_funder(
        self,
        org: dict,
        details: dict
    ) -> Optional[GrantOpportunity]:
        """
        Evaluates whether an organization from the 990 data is a relevant
        funder for the org profile and builds a GrantOpportunity object.

        This does not return open RFPs — it returns foundations that are
        strong relationship-building and prospecting targets based on their
        giving history.

        Args:
            org:     Basic organization data from the search results.
            details: Detailed 990 filing data for the organization.

        Returns:
            GrantOpportunity object if relevant, None otherwise.
        """
        org_data   = details.get("organization", {})
        filings    = details.get("filings_with_data", [])

        name       = org_data.get("name") or org.get("name", "")
        ein        = org_data.get("ein") or org.get("ein", "")
        city       = org_data.get("city", "")
        state      = org_data.get("state", "")
        ntee_code  = org_data.get("ntee_code", "")
        subsection = org_data.get("subsection_code", "")

        # Skip if no name
        if not name:
            return None

        # Skip if this is the same organization as our client
        if self.profile.org_name.lower() in name.lower():
            return None

        # Get financial data from most recent filing
        total_giving    = 0
        total_revenue   = 0
        latest_year     = None

        if filings:
            latest = filings[0]
            total_giving  = latest.get("totgrnts", 0) or 0
            total_revenue = latest.get("totrevenue", 0) or 0
            latest_year   = latest.get("tax_prd_yr")

        # Skip organizations with very low giving
        # These are unlikely to be meaningful grant sources
        if total_giving < self.MIN_TOTAL_GIVING:
            return None

        # Skip non-foundation organizations
        # We want foundations (subsection 3 = 501c3) that make grants
        # Subsection codes 3 and 4 cover most private foundations
        if subsection and str(subsection) not in ["3", "4", "92"]:
            return None

        # Build a description of the funder
        description = self._build_funder_description(
            name, city, state, ntee_code,
            total_giving, total_revenue, latest_year
        )

        # Format EIN for display
        ein_formatted = f"{ein[:2]}-{ein[2:]}" if len(ein) >= 9 else ein

        # Build the funder website URL
        propublica_url = f"https://projects.propublica.org/nonprofits/organizations/{ein}"

        return GrantOpportunity(
            funder_name              = name,
            program_name             = f"General Grantmaking — {latest_year or 'Recent'} Giving History",
            funder_website           = propublica_url,
            description              = description,
            geographic_focus         = f"{city}, {state}" if city else state,
            funding_type             = FundingType.GENERAL_OPERATING,

            # Status is UPCOMING not OPEN —
            # this is a prospecting target, not a current open RFP
            # The relationship mapping cycle uses these differently
            status                   = OpportunityStatus.UPCOMING,

            # No deadline — this is a relationship building target
            application_deadline     = None,

            # Use total giving as a proxy for potential award size
            award_max                = int(total_giving * 0.1) if total_giving else None,

            source_name              = self.name,
            source_url               = propublica_url,
            raw_data                 = {
                "ein":           ein_formatted,
                "ntee_code":     ntee_code,
                "total_giving":  total_giving,
                "total_revenue": total_revenue,
                "latest_year":   latest_year,
                "city":          city,
                "state":         state,
            }
        )

    def _build_funder_description(
        self,
        name: str,
        city: str,
        state: str,
        ntee_code: str,
        total_giving: int,
        total_revenue: int,
        latest_year: Optional[int]
    ) -> str:
        """
        Builds a plain-English description of a funder based on their
        990 data. Used in the prospect list output.

        Args:
            name:          Organization name
            city:          City of operation
            state:         State of operation
            ntee_code:     NTEE classification code
            total_giving:  Total grants awarded in latest filing year
            total_revenue: Total revenue in latest filing year
            latest_year:   Most recent filing year

        Returns:
            Plain-English description string.
        """
        location = f"{city}, {state}" if city else state
        giving_str = f"${total_giving:,}" if total_giving else "amount not reported"
        year_str = str(latest_year) if latest_year else "recent"

        return (
            f"{name} is a foundation based in {location}. "
            f"In {year_str}, they awarded {giving_str} in grants. "
            f"Their giving history suggests potential alignment with "
            f"{self.profile.org_short_name}'s mission and programs. "
            f"This organization was identified through IRS 990 data analysis "
            f"as a prospecting target for relationship development. "
            f"Review their full giving history to assess fit before outreach."
        )