"""
agent/profile.py
----------------
OrgProfile — the organizational profile schema for the grant prospecting agent.

This is the single source of truth for everything nonprofit-specific.
The engine reads this model and configures itself. No org-specific logic
lives anywhere else in the codebase.

Switching to a new nonprofit = loading a new profile JSON. Nothing else changes.

Usage:
    from agent.profile import OrgProfile
    profile = OrgProfile.from_json("profiles/deborah_place.json")
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class FunderType(str, Enum):
    """Categories of funders the agent will encounter."""
    PRIVATE_FOUNDATION   = "private_foundation"
    COMMUNITY_FOUNDATION = "community_foundation"
    CORPORATE            = "corporate"
    GOVERNMENT_FEDERAL   = "government_federal"
    GOVERNMENT_STATE     = "government_state"
    GOVERNMENT_LOCAL     = "government_local"
    RELIGIOUS            = "religious"
    PUBLIC_CHARITY       = "public_charity"
    UNKNOWN              = "unknown"


class ProgramArea(str, Enum):
    """
    Standardized program area taxonomy.
    Each area maps to a keyword cluster in the keyword mapper.
    """
    HOUSING_PERMANENT        = "housing_permanent"
    HOUSING_TRANSITIONAL     = "housing_transitional"
    HOUSING_RAPID_REHOUSING  = "housing_rapid_rehousing"
    DOMESTIC_VIOLENCE        = "domestic_violence"
    WORKFORCE_DEVELOPMENT    = "workforce_development"
    FOOD_SECURITY            = "food_security"
    HEALTHCARE               = "healthcare"
    MENTAL_HEALTH            = "mental_health"
    SUBSTANCE_USE            = "substance_use"
    REENTRY                  = "reentry"
    LEGAL_SERVICES           = "legal_services"
    CHILDCARE                = "childcare"
    EDUCATION                = "education"
    FINANCIAL_LITERACY       = "financial_literacy"
    GENERAL_OPERATING        = "general_operating"


class PopulationServed(str, Enum):
    """
    Standardized population taxonomy.
    Used to match funder eligibility requirements against the org's work.
    """
    WOMEN                   = "women"
    MEN                     = "men"
    YOUTH                   = "youth"
    SENIORS                 = "seniors"
    FAMILIES                = "families"
    VETERANS                = "veterans"
    LGBTQ                   = "lgbtq"
    IMMIGRANTS              = "immigrants"
    FORMERLY_INCARCERATED   = "formerly_incarcerated"
    SURVIVORS_DV            = "survivors_dv"
    CHRONICALLY_HOMELESS    = "chronically_homeless"
    LOW_INCOME              = "low_income"
    DISABLED                = "disabled"
    BIPOC                   = "bipoc"
    MENTAL_HEALTH           = "mental_health"


# ─────────────────────────────────────────────────────────────────────────────
# Sub-models
# ─────────────────────────────────────────────────────────────────────────────

class Geography(BaseModel):
    """Defines where the organization operates."""
    city:     str           = Field(..., description="Primary city of operation")
    state:    str           = Field(..., description="Two-letter state code e.g. IL")
    county:   Optional[str] = Field(None, description="County if relevant")
    region:   Optional[str] = Field(None, description="Regional descriptor")
    national: bool          = Field(False, description="True if org operates nationally")

    @field_validator("state")
    @classmethod
    def state_must_be_two_letters(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 2:
            raise ValueError(f"State must be a two-letter code, got: '{v}'")
        return v


class BudgetParameters(BaseModel):
    """
    Defines the org's typical grant request range.
    Budget fit is the most heavily weighted scoring criterion (2x).
    """
    request_floor:   int           = Field(..., gt=0, description="Minimum grant amount in USD")
    request_ceiling: int           = Field(..., gt=0, description="Maximum grant amount in USD")
    annual_budget:   Optional[int] = Field(None, gt=0, description="Total annual operating budget in USD")

    @model_validator(mode="after")
    def ceiling_must_exceed_floor(self) -> BudgetParameters:
        if self.request_ceiling <= self.request_floor:
            raise ValueError(
                f"request_ceiling ({self.request_ceiling}) must be greater than "
                f"request_floor ({self.request_floor})"
            )
        return self


class KnownFunder(BaseModel):
    """
    A foundation that has previously funded this organization.
    Used by the relationship mapping cycle to flag warm leads.
    """
    name:              str            = Field(..., description="Full legal name of the foundation")
    last_award_year:   Optional[int]  = Field(None, description="Most recent year a grant was received")
    last_award_amount: Optional[int]  = Field(None, description="Most recent grant amount in USD")
    funder_type:       FunderType     = Field(FunderType.UNKNOWN, description="Category of funder")
    notes:             Optional[str]  = Field(None, description="Relationship notes")


class AgentSettings(BaseModel):
    """
    Configuration flags that control how the agent behaves for this org.
    Toggleable by Admin through the portal.
    """
    exclude_federal:       bool  = Field(True,      description="Filter out federal funding opportunities")
    exclude_state:         bool  = Field(False,     description="Filter out state funding opportunities")
    deadline_floor_days:   int   = Field(14,        description="Minimum days until deadline")
    deadline_ceiling_days: int   = Field(365,       description="Maximum days until deadline")
    min_composite_score:   float = Field(2.0,       description="Minimum score to appear in results")
    discovery_cycle_day:   str   = Field("monday",  description="Day of week the discovery cycle runs")
    relationship_map_day:  int   = Field(1,         description="Day of month the relationship mapping cycle runs")


# ─────────────────────────────────────────────────────────────────────────────
# OrgProfile — the main model
# ─────────────────────────────────────────────────────────────────────────────

class OrgProfile(BaseModel):
    """
    The complete organizational profile for a nonprofit using the agent.

    This model is the ONLY place where nonprofit-specific information lives.
    The agent engine reads this at startup and configures all three operating
    cycles, the qualification matrix, and scoring weights from it.

    To onboard a new nonprofit:
        1. Copy profiles/org_profile_template.json
        2. Fill in the fields
        3. Run: python3 run_agent.py --profile profiles/new_org.json
        Nothing else changes.
    """

    # Identity
    org_name:       str           = Field(..., description="Full legal name of the organization")
    org_short_name: str           = Field(..., description="Short name used in reports and logs")
    ein:            Optional[str] = Field(None, description="IRS EIN e.g. 36-1234567")
    ntee_codes:     list[str]     = Field(default_factory=list, description="NTEE codes e.g. L41")
    website:        Optional[str] = Field(None, description="Organization website URL")
    founded_year:   Optional[int] = Field(None, description="Year the organization was founded")

    # Mission
    mission_statement: str       = Field(..., description="Full mission statement")
    mission_keywords:  list[str] = Field(default_factory=list, description="Additional search keywords")

    # Programs
    program_areas:        list[ProgramArea]    = Field(..., min_length=1, description="Active program areas")
    program_descriptions: dict[str, str]       = Field(default_factory=dict, description="Program descriptions keyed by name")

    # Population
    populations_served: list[PopulationServed] = Field(..., min_length=1, description="Populations the org serves")

    # Geography
    geography: Geography = Field(..., description="Where the organization operates")

    # Budget
    budget: BudgetParameters = Field(..., description="Grant request parameters")

    # Funder relationships
    known_funders:          list[KnownFunder]  = Field(default_factory=list, description="Previously funded by these orgs")
    funder_exclusions:      list[str]          = Field(default_factory=list, description="Funders to exclude from results")
    funder_type_exclusions: list[FunderType]   = Field(default_factory=list, description="Funder categories to exclude")

    # Agent settings
    settings: AgentSettings = Field(default_factory=AgentSettings, description="Agent behavior configuration")

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("ein")
    @classmethod
    def validate_ein_format(cls, v: Optional[str]) -> Optional[str]:
        """EIN must be in format XX-XXXXXXX if provided."""
        if v is None:
            return v
        v = v.strip()
        if not re.match(r"^\d{2}-\d{7}$", v):
            raise ValueError(f"EIN must be in format XX-XXXXXXX, got: '{v}'")
        return v

    @field_validator("ntee_codes")
    @classmethod
    def validate_ntee_codes(cls, v: list[str]) -> list[str]:
        """NTEE codes must be one letter followed by 1-3 digits e.g. L41."""
        validated = []
        for code in v:
            code = code.strip().upper()
            if not re.match(r"^[A-Z]\d{1,3}$", code):
                raise ValueError(f"Invalid NTEE code '{code}'. Expected e.g. 'L41'.")
            validated.append(code)
        return validated

    @field_validator("mission_statement")
    @classmethod
    def mission_must_be_substantive(cls, v: str) -> str:
        """Mission statement must be at least 20 characters."""
        v = v.strip()
        if len(v) < 20:
            raise ValueError(f"Mission statement must be at least 20 characters.")
        return v

    # ── Class methods ─────────────────────────────────────────────────────────

    @classmethod
    def from_json(cls, path: str | Path) -> OrgProfile:
        """
        Load and validate an OrgProfile from a JSON file.

        Args:
            path: Path to the profile JSON file

        Returns:
            A validated OrgProfile instance

        Raises:
            FileNotFoundError: If the file does not exist
            ValidationError:   If the JSON does not match the schema
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Profile file not found: '{path}'")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.model_validate(data)

    @classmethod
    def find_for_org(
        cls,
        org_name:     str,
        profiles_dir: str | Path = "profiles",
    ) -> Optional[OrgProfile]:
        """
        Locate and load the profile JSON for the given organization.

        Scans `profiles_dir` for any *.json (skipping the template), loads each
        one defensively, and returns the first profile whose org_name matches
        case-insensitively. Returns None if the directory is missing or no
        matching profile is found. Designed as the single source of truth for
        the router layer — replaces the previously duplicated `_load_profile`
        helpers in admin/results/feedback routers.

        Args:
            org_name:     The organization name to match (case-insensitive).
            profiles_dir: Directory containing profile JSON files.

        Returns:
            The matching OrgProfile, or None if not found.
        """
        profiles_path = Path(profiles_dir)
        if not profiles_path.exists():
            return None

        for profile_file in profiles_path.glob("*.json"):
            if profile_file.name == "org_profile_template.json":
                continue
            try:
                profile = cls.from_json(profile_file)
            except Exception:
                # Malformed or partial profile — skip silently and keep scanning.
                continue
            if profile.org_name.lower() == org_name.lower():
                return profile

        return None

    def to_json(self, path: str | Path, indent: int = 2) -> None:
        """Save this OrgProfile to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=indent))

    def get_active_funder_type_exclusions(self) -> list[FunderType]:
        """
        Returns the combined list of funder type exclusions.
        Merges explicit profile exclusions with settings-driven federal/state flags.
        The eligibility filter calls this — never reads the raw fields directly.
        """
        exclusions = list(self.funder_type_exclusions)
        if self.settings.exclude_federal:
            exclusions.append(FunderType.GOVERNMENT_FEDERAL)
        if self.settings.exclude_state:
            exclusions.append(FunderType.GOVERNMENT_STATE)
        seen = set()
        return [x for x in exclusions if not (x in seen or seen.add(x))]

    def summary(self) -> str:
        """
        Returns a concise human-readable summary of the profile.
        Used in logs and the agent's system prompt builder.
        """
        return (
            f"Organization:     {self.org_name}\n"
            f"Mission:          {self.mission_statement[:120]}...\n"
            f"Programs:         {', '.join(p.value for p in self.program_areas)}\n"
            f"Populations:      {', '.join(p.value for p in self.populations_served)}\n"
            f"Geography:        {self.geography.city}, {self.geography.state}\n"
            f"Grant range:      ${self.budget.request_floor:,} – ${self.budget.request_ceiling:,}\n"
            f"Federal excluded: {self.settings.exclude_federal}\n"
            f"Known funders:    {len(self.known_funders)}\n"
        )