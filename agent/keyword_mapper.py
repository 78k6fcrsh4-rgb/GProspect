"""
agent/keyword_mapper.py
-----------------------
KeywordMapper — translates an OrgProfile into targeted search queries.

The agent never hardcodes search terms. Instead it reads the org profile
and builds search queries dynamically from the program areas, populations
served, and geography declared in the profile.

This means the same engine works for any nonprofit — a food bank in Atlanta
gets food-security search terms, a veterans org in Denver gets veterans
housing terms. The mapper configures itself from the profile automatically.

Usage:
    from agent.keyword_mapper import KeywordMapper
    from agent.profile import OrgProfile

    profile = OrgProfile.from_json("profiles/deborah_place.json")
    mapper  = KeywordMapper(profile)

    # Get all search queries for the monitoring cycle
    queries = mapper.build_search_queries()

    # Get queries for a specific program area
    housing_queries = mapper.queries_for_program("housing_permanent")
"""

from __future__ import annotations

from agent.profile import OrgProfile, ProgramArea, PopulationServed


# ─────────────────────────────────────────────────────────────────────────────
# Base keyword clusters
# Each program area maps to a list of core search phrases.
# These are the building blocks — the mapper combines them with geography
# and population terms to create the final targeted queries.
# ─────────────────────────────────────────────────────────────────────────────

PROGRAM_KEYWORDS: dict[ProgramArea, list[str]] = {

    ProgramArea.HOUSING_PERMANENT: [
        "permanent supportive housing grants",
        "PSH funding nonprofit",
        "permanent affordable housing women",
        "housing stability grants",
        "supportive housing foundation funding",
        "long-term housing assistance grants",
    ],

    ProgramArea.HOUSING_TRANSITIONAL: [
        "transitional housing grants",
        "transitional housing women funding",
        "temporary housing nonprofit grants",
        "bridge housing funding",
        "transitional shelter grants",
        "housing transition program funding",
    ],

    ProgramArea.HOUSING_RAPID_REHOUSING: [
        "rapid rehousing grants",
        "rapid rehousing program funding",
        "emergency housing assistance grants",
        "short-term housing subsidy grants",
        "housing crisis intervention funding",
    ],

    ProgramArea.DOMESTIC_VIOLENCE: [
        "domestic violence grants",
        "DV services nonprofit funding",
        "gender-based violence prevention grants",
        "survivor support services funding",
        "domestic violence shelter grants",
        "intimate partner violence program funding",
        "trauma-informed services grants",
    ],

    ProgramArea.WORKFORCE_DEVELOPMENT: [
        "workforce development grants",
        "job training nonprofit funding",
        "economic mobility grants women",
        "workforce training program funding",
        "employment services grants",
        "career development nonprofit grants",
        "job placement program funding",
        "vocational training grants",
    ],

    ProgramArea.FOOD_SECURITY: [
        "food security grants nonprofit",
        "food pantry funding",
        "hunger relief grants",
        "nutrition program nonprofit funding",
        "food access grants",
        "meal program nonprofit funding",
    ],

    ProgramArea.HEALTHCARE: [
        "healthcare access grants nonprofit",
        "community health program funding",
        "healthcare navigation grants",
        "primary care access nonprofit grants",
        "health services funding women",
        "preventive health grants",
    ],

    ProgramArea.MENTAL_HEALTH: [
        "mental health services grants",
        "trauma-informed care funding",
        "mental health nonprofit grants",
        "behavioral health program funding",
        "psychiatric services nonprofit grants",
        "mental health counseling grants",
    ],

    ProgramArea.SUBSTANCE_USE: [
        "substance use treatment grants",
        "addiction recovery nonprofit funding",
        "substance abuse program grants",
        "recovery services funding",
        "harm reduction grants",
    ],

    ProgramArea.REENTRY: [
        "reentry services grants",
        "formerly incarcerated women funding",
        "criminal justice reentry grants",
        "justice-involved women nonprofit funding",
        "reintegration program grants",
    ],

    ProgramArea.LEGAL_SERVICES: [
        "legal services nonprofit grants",
        "civil legal aid funding",
        "legal advocacy grants women",
        "pro bono legal services funding",
        "legal assistance nonprofit grants",
    ],

    ProgramArea.CHILDCARE: [
        "childcare program grants",
        "early childhood services funding",
        "childcare nonprofit grants",
        "child development program funding",
    ],

    ProgramArea.EDUCATION: [
        "adult education grants nonprofit",
        "literacy program funding",
        "GED program grants",
        "continuing education nonprofit grants",
        "education access grants women",
    ],

    ProgramArea.FINANCIAL_LITERACY: [
        "financial literacy program grants",
        "financial empowerment nonprofit funding",
        "economic empowerment grants women",
        "financial coaching program grants",
    ],

    ProgramArea.GENERAL_OPERATING: [
        "general operating support grants",
        "unrestricted funding nonprofit",
        "general operating grants nonprofit",
        "capacity building grants",
        "core operating support funding",
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Population modifier phrases
# These get combined with program keywords to narrow searches to the
# right population. e.g. "workforce development grants" becomes
# "workforce development grants women experiencing homelessness"
# ─────────────────────────────────────────────────────────────────────────────

POPULATION_MODIFIERS: dict[PopulationServed, list[str]] = {
    PopulationServed.WOMEN:                 ["women", "women-led", "gender-responsive"],
    PopulationServed.CHRONICALLY_HOMELESS:  ["chronically homeless", "experiencing homelessness", "unhoused"],
    PopulationServed.SURVIVORS_DV:          ["domestic violence survivors", "DV survivors", "trauma survivors"],
    PopulationServed.LOW_INCOME:            ["low-income", "economically disadvantaged", "poverty"],
    PopulationServed.BIPOC:                 ["BIPOC", "communities of color", "racial equity"],
    PopulationServed.MENTAL_HEALTH:         ["mental health", "behavioral health", "psychiatric"],
    PopulationServed.FORMERLY_INCARCERATED: ["formerly incarcerated", "justice-involved", "reentry"],
    PopulationServed.VETERANS:              ["veterans", "military veterans", "veteran services"],
    PopulationServed.YOUTH:                 ["youth", "young adults", "at-risk youth"],
    PopulationServed.SENIORS:               ["seniors", "older adults", "elderly"],
    PopulationServed.FAMILIES:              ["families", "family services", "family stability"],
    PopulationServed.LGBTQ:                 ["LGBTQ", "LGBTQ+", "gender diverse"],
    PopulationServed.IMMIGRANTS:            ["immigrants", "refugees", "asylum seekers"],
    PopulationServed.DISABLED:              ["disabled", "disability services", "accessibility"],
    PopulationServed.MEN:                   ["men", "male-serving"],
}


# ─────────────────────────────────────────────────────────────────────────────
# KeywordMapper
# ─────────────────────────────────────────────────────────────────────────────

class KeywordMapper:
    """
    Translates an OrgProfile into targeted grant search queries.

    The mapper reads the active program areas, populations served, and
    geography from the profile and builds three types of search queries:

    1. Base queries      — program area keywords alone
    2. Population queries — program keywords + population modifiers
    3. Geo queries       — program keywords + city/state context

    All three types are combined into the final query list the agent uses
    when running the monitoring and discovery cycles.
    """

    def __init__(self, profile: OrgProfile) -> None:
        """
        Initialize the mapper with a validated OrgProfile.

        Args:
            profile: A loaded and validated OrgProfile instance.
        """
        self.profile    = profile
        self.city       = profile.geography.city
        self.state      = profile.geography.state
        self.region     = profile.geography.region or f"{self.city} metro"

        # Pull the primary population modifier — used most frequently
        # We use the first listed population as the primary descriptor
        self.primary_population = self._get_primary_population_phrase()

    # ── Public methods ────────────────────────────────────────────────────────

    def build_search_queries(self) -> list[str]:
        """
        Build the complete list of search queries for this org profile.

        Returns a deduplicated list of search strings ready to be passed
        to the web search tool, grant database tools, and discovery cycle.

        Returns:
            List of search query strings, deduplicated and ordered by
            specificity — most targeted queries first.

        Example:
            queries = mapper.build_search_queries()
            # Returns:
            # [
            #   "permanent supportive housing grants women Chicago",
            #   "transitional housing grants domestic violence survivors Illinois",
            #   "workforce development grants women experiencing homelessness",
            #   ...
            # ]
        """
        all_queries: list[str] = []

        for program_area in self.profile.program_areas:
            # Get base keywords for this program area
            # If somehow a program area has no keywords, skip it gracefully
            base_keywords = PROGRAM_KEYWORDS.get(program_area, [])
            if not base_keywords:
                continue

            # Build three layers of queries for each keyword
            for keyword in base_keywords:

                # Layer 1 — Base query with geography
                # e.g. "permanent supportive housing grants Chicago Illinois"
                all_queries.append(
                    f"{keyword} {self.city} {self.state}"
                )

                # Layer 2 — Primary population + geography
                # e.g. "permanent supportive housing grants women Chicago"
                if self.primary_population:
                    all_queries.append(
                        f"{keyword} {self.primary_population} {self.city}"
                    )

                # Layer 3 — Base query with region (broader geographic net)
                # e.g. "permanent supportive housing grants Chicago metro"
                all_queries.append(
                    f"{keyword} {self.region}"
                )

        # Add mission keyword queries — these catch funders aligned
        # with the org's specific language and framing
        for mission_kw in self.profile.mission_keywords:
            all_queries.append(f"{mission_kw} grants {self.city}")
            all_queries.append(f"{mission_kw} foundation funding")

        # Add cross-program combination queries
        # These catch funders who look for integrated service models
        all_queries.extend(self._build_combination_queries())

        # Deduplicate while preserving order
        seen    = set()
        unique  = []
        for q in all_queries:
            q_clean = q.strip().lower()
            if q_clean not in seen:
                seen.add(q_clean)
                unique.append(q.strip())

        return unique

    def queries_for_program(self, program_area: str | ProgramArea) -> list[str]:
        """
        Get search queries for a single program area only.

        Useful when the monitoring cycle wants to do a targeted search
        for one specific program rather than the full query set.

        Args:
            program_area: Either a ProgramArea enum value or its string value
                         e.g. ProgramArea.HOUSING_PERMANENT or "housing_permanent"

        Returns:
            List of search query strings for that program area.

        Example:
            queries = mapper.queries_for_program("workforce_development")
        """
        # Accept either the enum or its string value
        if isinstance(program_area, str):
            try:
                program_area = ProgramArea(program_area)
            except ValueError:
                raise ValueError(
                    f"Unknown program area: '{program_area}'. "
                    f"Valid values: {[p.value for p in ProgramArea]}"
                )

        base_keywords = PROGRAM_KEYWORDS.get(program_area, [])
        queries = []
        for keyword in base_keywords:
            queries.append(f"{keyword} {self.city} {self.state}")
            if self.primary_population:
                queries.append(f"{keyword} {self.primary_population} {self.city}")

        return queries

    def get_funder_search_queries(self) -> list[str]:
        """
        Build queries specifically for finding NEW funders in the discovery cycle.

        These are broader than opportunity-specific queries — they look for
        foundations and philanthropic organizations aligned with the org's
        mission rather than specific open RFPs.

        Returns:
            List of funder discovery search queries.
        """
        queries = []
        city    = self.city
        state   = self.state

        # Foundation discovery queries by program area
        for program_area in self.profile.program_areas:
            base_keywords = PROGRAM_KEYWORDS.get(program_area, [])
            if base_keywords:
                # Use just the first two keywords per area for discovery
                # We want breadth here, not depth
                for keyword in base_keywords[:2]:
                    queries.append(f"foundation funding {keyword} {city}")
                    queries.append(f"private foundation grants {keyword} {state}")
                    queries.append(f"philanthropy {keyword} {city} nonprofit")

        # Queries targeting the org's mission keywords directly
        for kw in self.profile.mission_keywords[:5]:
            queries.append(f"foundation {kw} {city}")
            queries.append(f"grants {kw} nonprofit {state}")

        # General nonprofit landscape queries for this geography
        queries.extend([
            f"private foundations {city} housing grants",
            f"community foundations {city} social services",
            f"philanthropy {city} women homelessness",
            f"new grant opportunities {city} {self.primary_population}",
            f"RFP {city} nonprofit {self.profile.program_areas[0].value.replace('_', ' ')}",
        ])

        # Deduplicate
        seen   = set()
        unique = []
        for q in queries:
            q_clean = q.strip().lower()
            if q_clean not in seen:
                seen.add(q_clean)
                unique.append(q.strip())

        return unique

    def summary(self) -> str:
        """
        Returns a human-readable summary of what the mapper will search for.
        Useful for logging and debugging.
        """
        total_queries = len(self.build_search_queries())
        funder_queries = len(self.get_funder_search_queries())
        return (
            f"KeywordMapper for: {self.profile.org_name}\n"
            f"Active program areas: {len(self.profile.program_areas)}\n"
            f"Total monitoring queries: {total_queries}\n"
            f"Total discovery queries:  {funder_queries}\n"
            f"Primary population:       {self.primary_population}\n"
            f"Geography:                {self.city}, {self.state}\n"
        )

    # ── Private methods ───────────────────────────────────────────────────────

    def _get_primary_population_phrase(self) -> str:
        """
        Returns the most descriptive population phrase for this org.
        Used to add population context to search queries.

        Picks the first population from the profile's populations_served
        list that has a defined modifier phrase.
        """
        for population in self.profile.populations_served:
            modifiers = POPULATION_MODIFIERS.get(population)
            if modifiers:
                # Return the first (most common) modifier phrase
                return modifiers[0]
        return ""

    def _build_combination_queries(self) -> list[str]:
        """
        Builds cross-program combination queries that catch funders who
        look for integrated service models rather than single program areas.

        For example a funder interested in "housing AND workforce" is more
        likely to fund Deborah's Place than one interested in housing alone.
        These queries surface those funders.
        """
        queries     = []
        areas       = self.profile.program_areas
        city        = self.city
        population  = self.primary_population

        # Only build combination queries if org has multiple program areas
        # Single-program orgs do not benefit from these
        if len(areas) < 2:
            return queries

        # Housing + workforce combination — very common funder interest area
        if (ProgramArea.HOUSING_PERMANENT in areas or
                ProgramArea.HOUSING_TRANSITIONAL in areas) and \
                ProgramArea.WORKFORCE_DEVELOPMENT in areas:
            queries.extend([
                f"housing workforce development grants {city}",
                f"housing economic mobility grants {population} {city}",
                f"integrated housing services grants {city}",
            ])

        # Housing + domestic violence combination
        if (ProgramArea.HOUSING_PERMANENT in areas or
                ProgramArea.HOUSING_TRANSITIONAL in areas) and \
                ProgramArea.DOMESTIC_VIOLENCE in areas:
            queries.extend([
                f"housing domestic violence survivor grants {city}",
                f"safe housing DV services nonprofit funding {city}",
                f"gender-responsive housing grants {city}",
            ])

        # Housing + mental health combination
        if (ProgramArea.HOUSING_PERMANENT in areas or
                ProgramArea.HOUSING_TRANSITIONAL in areas) and \
                ProgramArea.MENTAL_HEALTH in areas:
            queries.extend([
                f"supportive housing mental health grants {city}",
                f"housing behavioral health services funding {city}",
            ])

        # General wraparound services query — catches funders looking
        # for comprehensive service models
        if len(areas) >= 3:
            queries.extend([
                f"wraparound services grants {population} {city}",
                f"comprehensive services nonprofit funding {city}",
                f"holistic services women homelessness grants {city}",
            ])

        return queries