"""
frontend/intake.py
------------------
Streamlit intake wizard — 10-step long-and-narrow profile capture.

Flow:
  - Step 0 (top of wizard, not in step counter): doc-assist prefill.
    User can upload a case statement / strategic plan / 990 narrative
    and Claude pre-populates the wizard fields.
  - Steps 1-10: identity, mission, NTEE codes, programs, populations,
    geography, budget, known funders, exclusions, agent settings.
  - Final step's "Save & Submit" posts to /orgs/me/profile/version which
    validates the full payload against the Pydantic OrgProfile schema
    and either creates a new version (201) or returns 422 with errors.

The wizard reads from and writes to `st.session_state.wizard_payload`,
a dict shaped like OrgProfile. The current step is `wizard_step`. Each
field's widget has a stable `key` so the value persists across reruns.

Render entry point: `render_intake(api, user)` from streamlit_app.py.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.api import APIError, GProspectAPI


# ─────────────────────────────────────────────────────────────────────────────
# Enum values — mirror agent/profile.py. Kept in the frontend so the wizard
# doesn't need to import backend modules.
# ─────────────────────────────────────────────────────────────────────────────

PROGRAM_AREAS = [
    "housing_permanent",
    "housing_transitional",
    "housing_rapid_rehousing",
    "domestic_violence",
    "workforce_development",
    "food_security",
    "healthcare",
    "mental_health",
    "substance_use",
    "reentry",
    "legal_services",
    "childcare",
    "education",
    "financial_literacy",
    "general_operating",
]

POPULATIONS_SERVED = [
    "women", "men", "youth", "seniors", "families", "veterans", "lgbtq",
    "immigrants", "formerly_incarcerated", "survivors_dv",
    "chronically_homeless", "low_income", "disabled", "bipoc", "mental_health",
]

FUNDER_TYPES = [
    "private_foundation", "community_foundation", "corporate",
    "government_federal", "government_state", "government_local",
    "religious", "public_charity", "unknown",
]

TOTAL_STEPS = 10


# ─────────────────────────────────────────────────────────────────────────────
# Defaults — used when the wizard starts from a blank slate
# ─────────────────────────────────────────────────────────────────────────────

def _empty_payload() -> dict:
    return {
        "org_name":             "",
        "org_short_name":       "",
        "ein":                  None,
        "ntee_codes":           [],
        "website":              None,
        "founded_year":         None,
        "mission_statement":    "",
        "mission_keywords":     [],
        "program_areas":        [],
        "program_descriptions": {},
        "populations_served":   [],
        "geography": {
            "city": "", "state": "", "county": None,
            "region": None, "national": False,
        },
        "budget": {
            "request_floor":   25000,
            "request_ceiling": 250000,
            "annual_budget":   None,
        },
        "known_funders":          [],
        "funder_exclusions":      [],
        "funder_type_exclusions": [],
        "settings": {
            "exclude_federal":       True,
            "exclude_state":         False,
            "deadline_floor_days":   14,
            "deadline_ceiling_days": 365,
            "min_composite_score":   2.0,
            "discovery_cycle_day":   "monday",
            "relationship_map_day":  1,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def render_intake(api: GProspectAPI, user: dict) -> None:
    """Top-level wizard renderer. Called from streamlit_app's router."""
    _ensure_wizard_state(api)

    st.title("📝 Profile Intake")
    st.caption(
        "Build the organizational profile the agent uses for funder matching. "
        "Long-and-narrow wizard — one screen at a time. Use the prefill button "
        "below to skip the typing if you have a case statement or strategic plan."
    )

    _render_doc_assist(api)

    # Step gauge
    step = st.session_state.wizard_step
    progress = step / TOTAL_STEPS
    st.progress(progress, text=f"Step {step} of {TOTAL_STEPS}")

    # Dispatch
    STEPS[step]()

    # Navigation
    _render_nav(api)


# ─────────────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_wizard_state(api: GProspectAPI) -> None:
    """Populate session_state.wizard_payload on first visit."""
    if "wizard_step" not in st.session_state:
        st.session_state.wizard_step = 1
    if "wizard_payload" not in st.session_state:
        # Bootstrap from the current saved profile if one exists, else empty.
        current = None
        try:
            current = api.get_current_profile()
        except APIError as e:
            st.warning(f"Could not load existing profile: {e.detail}")
        st.session_state.wizard_payload = (
            current["payload"] if current else _empty_payload()
        )
    if "extraction_notes" not in st.session_state:
        st.session_state.extraction_notes = []
    if "last_save_error" not in st.session_state:
        st.session_state.last_save_error = None


# ─────────────────────────────────────────────────────────────────────────────
# Doc-assist
# ─────────────────────────────────────────────────────────────────────────────

def _render_doc_assist(api: GProspectAPI) -> None:
    with st.expander(
        "📄 Have a case statement, strategic plan, 990 narrative, or annual "
        "report? Upload it to prefill this form.",
        expanded = False,
    ):
        st.caption(
            "Supported: .docx, .pdf, .txt, .md (up to 5 MB). Claude reads it "
            "and pre-fills whatever it can extract. You'll review every field "
            "before saving — extraction is a starting point, not a final answer."
        )
        uploaded = st.file_uploader(
            "Choose a file",
            type = ["docx", "pdf", "txt", "md"],
            key  = "doc_assist_upload",
            accept_multiple_files = False,
        )
        if uploaded is not None and st.button("Extract from this document",
                                              type="primary"):
            with st.spinner("Reading your document…"):
                try:
                    result = api.extract_profile_from_doc(
                        file_bytes = uploaded.getvalue(),
                        filename   = uploaded.name,
                        mime_type  = uploaded.type or "application/octet-stream",
                    )
                except APIError as e:
                    st.error(f"Extraction failed: {e.detail}")
                    return

            extracted = result.get("extracted_fields") or {}
            if not extracted:
                st.warning(
                    "No fields could be extracted from this document. "
                    "Fill in the wizard manually."
                )
            else:
                # Merge — extracted fields overwrite empty values but preserve
                # anything the user has already typed.
                merged = _merge_payloads(
                    base    = st.session_state.wizard_payload,
                    overlay = extracted,
                )
                st.session_state.wizard_payload  = merged
                st.session_state.extraction_notes = result.get("notes") or []
                st.success(
                    f"Prefilled {len(extracted)} top-level field(s). "
                    f"Review each step before saving."
                )
                st.rerun()

    if st.session_state.extraction_notes:
        with st.container(border=True):
            st.markdown("**Doc-assist notes:**")
            for note in st.session_state.extraction_notes:
                st.markdown(f"- {note}")
            if st.button("Dismiss notes", key="dismiss_notes"):
                st.session_state.extraction_notes = []
                st.rerun()


def _merge_payloads(base: dict, overlay: dict) -> dict:
    """
    Shallow-merge overlay into base. For nested dicts (geography, budget,
    settings), merge keys individually. For lists, overlay replaces if
    non-empty. Strings/numbers in overlay only replace if base is empty/None.
    """
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_payloads(out[key], val)
        elif isinstance(val, list) and val:
            out[key] = val
        elif val not in (None, "", []):
            # Only overwrite if base value is empty/default.
            if out.get(key) in (None, "", [], {}, 0):
                out[key] = val
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Navigation
# ─────────────────────────────────────────────────────────────────────────────

def _render_nav(api: GProspectAPI) -> None:
    st.divider()
    step = st.session_state.wizard_step
    cols = st.columns([1, 1, 1, 2])

    with cols[0]:
        if step > 1:
            if st.button("← Back", use_container_width=True):
                st.session_state.wizard_step = step - 1
                st.rerun()

    with cols[1]:
        if step < TOTAL_STEPS:
            if st.button("Next →", type="primary", use_container_width=True):
                st.session_state.wizard_step = step + 1
                st.rerun()

    with cols[3]:
        if step == TOTAL_STEPS:
            if st.button("💾 Save & Submit", type="primary",
                         use_container_width=True):
                _do_save(api)

    if st.session_state.last_save_error:
        st.error(st.session_state.last_save_error)


def _do_save(api: GProspectAPI) -> None:
    payload = st.session_state.wizard_payload
    try:
        new_version = api.save_profile_version(payload)
    except APIError as e:
        st.session_state.last_save_error = (
            f"Save failed (HTTP {e.status_code}): {e.detail}"
        )
        return
    st.session_state.last_save_error = None
    st.session_state.extraction_notes = []
    st.success(
        f"✅ Saved as version {new_version['version']}. "
        f"You can keep editing or switch to the dashboard."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step renderers
# ─────────────────────────────────────────────────────────────────────────────

def _p() -> dict:
    """Shorthand for the session_state payload."""
    return st.session_state.wizard_payload


def step_1_identity() -> None:
    st.subheader("1. Organization identity")
    p = _p()
    p["org_name"] = st.text_input(
        "Legal name *",
        value = p.get("org_name", ""),
        help  = "Full legal name as it appears on your IRS determination letter.",
    )
    p["org_short_name"] = st.text_input(
        "Short / display name *",
        value = p.get("org_short_name", ""),
        help  = "The name your team uses day-to-day.",
    )
    p["ein"] = st.text_input(
        "EIN",
        value       = p.get("ein") or "",
        placeholder = "XX-XXXXXXX",
        help        = "IRS Employer Identification Number. Optional but strongly "
                      "recommended — unlocks 990-data funder matching.",
    ) or None
    p["website"] = st.text_input(
        "Website",
        value       = p.get("website") or "",
        placeholder = "https://www.yourorg.org",
    ) or None
    founded = st.number_input(
        "Founded year",
        min_value = 1700, max_value = 2030, step = 1,
        value     = int(p.get("founded_year") or 2000),
        help      = "Optional.",
    )
    p["founded_year"] = int(founded) if founded else None


def step_2_mission() -> None:
    st.subheader("2. Mission")
    p = _p()
    p["mission_statement"] = st.text_area(
        "Mission statement *",
        value  = p.get("mission_statement", ""),
        height = 140,
        help   = "Copy the full mission statement from your website or IRS "
                 "letter. Must be at least 20 characters. Used heavily by the "
                 "scoring engine.",
    )
    kw_str = ", ".join(p.get("mission_keywords") or [])
    new_kw = st.text_input(
        "Additional mission keywords",
        value       = kw_str,
        placeholder = "trauma-informed care, housing first, wraparound services",
        help        = "Comma-separated phrases. Help the agent find funders that "
                      "use different language for the same work.",
    )
    p["mission_keywords"] = [k.strip() for k in new_kw.split(",") if k.strip()]


def step_3_ntee() -> None:
    st.subheader("3. NTEE codes")
    p = _p()
    st.caption(
        "NTEE codes are the IRS classification system for nonprofits. Most "
        "orgs have 1–2 primary codes. Look yours up at "
        "[nccs.urban.org](https://nccs.urban.org/publication/irs-activity-codes)."
    )
    codes_str = ", ".join(p.get("ntee_codes") or [])
    new_codes = st.text_input(
        "NTEE codes (comma-separated)",
        value       = codes_str,
        placeholder = "P30, P40",
        help        = "Format: one letter + 1-3 digits. Common housing codes: L41 "
                      "(temporary housing), L80 (housing support). Common youth: "
                      "P30 (children/youth services), P40 (family services).",
    )
    p["ntee_codes"] = [c.strip().upper() for c in new_codes.split(",") if c.strip()]


def step_4_programs() -> None:
    st.subheader("4. Programs")
    p = _p()
    st.caption(
        "Select every active program area your organization runs. At least one "
        "is required. Then add a one-paragraph description for each so the "
        "agent can write specific search queries."
    )
    selected = st.multiselect(
        "Program areas *",
        options = PROGRAM_AREAS,
        default = p.get("program_areas") or [],
    )
    p["program_areas"] = selected

    if selected:
        st.markdown("**Program descriptions** (one per program area):")
        descs = dict(p.get("program_descriptions") or {})
        # Clean out any descriptions for unselected areas.
        descs = {k: v for k, v in descs.items() if k in selected}
        for area in selected:
            descs[area] = st.text_area(
                f"Description for `{area}`",
                value  = descs.get(area, ""),
                key    = f"w_prog_desc_{area}",
                height = 100,
                help   = "Plain-language description, the way you'd describe it "
                         "to a funder. Optional, but improves results.",
            )
        p["program_descriptions"] = descs


def step_5_populations() -> None:
    st.subheader("5. Populations served")
    p = _p()
    st.caption(
        "Pick every population your organization serves. At least one is "
        "required. The agent uses this to match funder eligibility criteria."
    )
    selected = st.multiselect(
        "Populations served *",
        options = POPULATIONS_SERVED,
        default = p.get("populations_served") or [],
    )
    p["populations_served"] = selected


def step_6_geography() -> None:
    st.subheader("6. Geography")
    p = _p()
    geo = dict(p.get("geography") or {})

    col1, col2 = st.columns(2)
    geo["city"]  = col1.text_input("City *",  value=geo.get("city")  or "")
    geo["state"] = col2.text_input("State *", value=geo.get("state") or "",
                                   max_chars=2,
                                   help="Two-letter code, e.g. IL, OH.")
    geo["county"] = st.text_input(
        "County",
        value       = geo.get("county") or "",
        placeholder = "e.g. Hamilton County",
    ) or None
    geo["region"] = st.text_input(
        "Regional descriptor",
        value       = geo.get("region") or "",
        placeholder = "e.g. Greater Cincinnati, Chicago metro",
        help        = "Used in search queries when local foundations describe "
                      "their geography in regional terms.",
    ) or None
    geo["national"] = st.checkbox(
        "Operates nationally across multiple states",
        value = bool(geo.get("national")),
    )
    p["geography"] = geo


def step_7_budget() -> None:
    st.subheader("7. Grant budget parameters")
    p = _p()
    budget = dict(p.get("budget") or {})
    st.caption(
        "Tell the agent what size grant requests to surface. Budget fit is the "
        "most heavily-weighted scoring criterion."
    )
    col1, col2 = st.columns(2)
    budget["request_floor"] = int(col1.number_input(
        "Minimum grant request (USD) *",
        min_value = 1000, step = 1000,
        value     = int(budget.get("request_floor") or 25000),
        help      = "Smallest grant worth your application time.",
    ))
    budget["request_ceiling"] = int(col2.number_input(
        "Maximum grant request (USD) *",
        min_value = 1000, step = 1000,
        value     = int(budget.get("request_ceiling") or 250000),
        help      = "Largest grant you can realistically receive and execute.",
    ))
    ab = budget.get("annual_budget")
    annual = st.number_input(
        "Annual operating budget (USD)",
        min_value = 0, step = 10000,
        value     = int(ab) if ab else 0,
        help      = "Optional. Used to assess whether you're the right-size "
                    "org for a given funder.",
    )
    budget["annual_budget"] = int(annual) if annual else None
    p["budget"] = budget


def step_8_known_funders() -> None:
    st.subheader("8. Known funders")
    p = _p()
    st.caption(
        "Foundations that have funded you in the past 5 years. The agent flags "
        "currently-open opportunities from these funders as warm leads."
    )
    funders = list(p.get("known_funders") or [])

    # Editable table — Streamlit's data_editor handles add/edit/remove.
    edited = st.data_editor(
        funders,
        num_rows = "dynamic",
        column_config = {
            "name":              st.column_config.TextColumn(   "Foundation name"),
            "last_award_year":   st.column_config.NumberColumn( "Last award year",
                                                                min_value=1900,
                                                                max_value=2100),
            "last_award_amount": st.column_config.NumberColumn( "Last award (USD)",
                                                                min_value=0),
            "funder_type":       st.column_config.SelectboxColumn(
                "Type", options=FUNDER_TYPES),
            "notes":             st.column_config.TextColumn(   "Notes"),
        },
        use_container_width = True,
        key                 = "w_known_funders",
    )

    # data_editor returns a list of dicts; drop entries with no name.
    cleaned = []
    for row in edited or []:
        if isinstance(row, dict) and (row.get("name") or "").strip():
            cleaned.append({
                "name":              row.get("name", "").strip(),
                "last_award_year":   row.get("last_award_year"),
                "last_award_amount": row.get("last_award_amount"),
                "funder_type":       row.get("funder_type") or "unknown",
                "notes":             row.get("notes") or None,
            })
    p["known_funders"] = cleaned


def step_9_exclusions() -> None:
    st.subheader("9. Funder exclusions")
    p = _p()
    st.caption("Optional. Funders you don't want to see in results.")

    exc_str = "\n".join(p.get("funder_exclusions") or [])
    new_exc = st.text_area(
        "Specific funders to exclude (one per line)",
        value       = exc_str,
        height      = 100,
        placeholder = "XYZ Foundation\nABC Family Fund",
    )
    p["funder_exclusions"] = [
        ln.strip() for ln in new_exc.splitlines() if ln.strip()
    ]

    p["funder_type_exclusions"] = st.multiselect(
        "Funder categories to exclude",
        options = FUNDER_TYPES,
        default = p.get("funder_type_exclusions") or [],
        help    = "Exclude entire categories — e.g. federal funding if you "
                  "don't want to deal with the compliance overhead.",
    )


def step_10_settings() -> None:
    st.subheader("10. Agent settings")
    p = _p()
    settings = dict(p.get("settings") or {})

    st.caption(
        "Fine-tune the agent's behavior. The defaults below work well for "
        "most orgs — adjust only if you have a reason."
    )

    col1, col2 = st.columns(2)
    settings["exclude_federal"] = col1.checkbox(
        "Exclude federal funding",
        value = bool(settings.get("exclude_federal", True)),
    )
    settings["exclude_state"] = col2.checkbox(
        "Exclude state funding",
        value = bool(settings.get("exclude_state", False)),
    )

    col3, col4 = st.columns(2)
    settings["deadline_floor_days"] = int(col3.number_input(
        "Minimum days to deadline",
        min_value = 1, max_value = 365, step = 1,
        value     = int(settings.get("deadline_floor_days") or 14),
        help      = "Opportunities closing sooner than this are hidden.",
    ))
    settings["deadline_ceiling_days"] = int(col4.number_input(
        "Maximum days to deadline",
        min_value = 30, max_value = 730, step = 30,
        value     = int(settings.get("deadline_ceiling_days") or 365),
    ))

    settings["min_composite_score"] = float(st.slider(
        "Minimum match score to show",
        min_value = 0.0, max_value = 5.0, step = 0.25,
        value     = float(settings.get("min_composite_score") or 2.0),
        help      = "Higher = fewer but stronger matches.",
    ))

    # Discovery cycle day + relationship map day are non-user-facing for now;
    # keep them at the defaults from the existing payload.
    settings.setdefault("discovery_cycle_day", "monday")
    settings.setdefault("relationship_map_day", 1)

    p["settings"] = settings

    # Summary preview before save
    st.divider()
    st.markdown("**Ready to save?**")
    st.caption(
        "Clicking Save & Submit below validates the full profile against the "
        "schema and creates a new version. If anything's invalid you'll see "
        "the field-level errors here before any save happens."
    )


# Step registry
STEPS: dict[int, Any] = {
    1:  step_1_identity,
    2:  step_2_mission,
    3:  step_3_ntee,
    4:  step_4_programs,
    5:  step_5_populations,
    6:  step_6_geography,
    7:  step_7_budget,
    8:  step_8_known_funders,
    9:  step_9_exclusions,
    10: step_10_settings,
}
