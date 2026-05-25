"""
frontend/streamlit_app.py
-------------------------
GProspect v2 — Streamlit frontend.

Three screens behind login:

  📝 Intake       — multi-step profile wizard with doc-assist prefill
                     (Phase 1a).
  🎯 Dashboard    — placeholder until Phase 1b ships card-based results.
  🕒 History      — list of saved profile versions (P1a nice-to-have).

The Streamlit app talks to the FastAPI portal at PORTAL_API_URL (default
http://localhost:8000). All HTTP goes through frontend.api.GProspectAPI
so error handling lives in one place.

Run:
    streamlit run frontend/streamlit_app.py
"""

from __future__ import annotations

# ── Bootstrap: ensure the repo root is on sys.path ───────────────────────────
# When launched via `streamlit run frontend/streamlit_app.py`, Streamlit only
# adds the script's directory (frontend/) to sys.path — not the repo root —
# so `from frontend.api import ...` fails with ModuleNotFoundError. Prepend
# the parent (the repo root) before any frontend.* imports. No-op when
# launched in any other way that already has the repo root on the path.
import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# ─────────────────────────────────────────────────────────────────────────────

import streamlit as st

from frontend.api       import API_URL, APIError, GProspectAPI
from frontend.intake    import render_intake
from frontend.prospects import render_pipeline, render_prospects
from frontend.funders   import render_funders


# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title = "GProspect",
    page_icon  = "🎯",
    layout     = "wide",
)


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

def _api() -> GProspectAPI:
    """Construct an API client using the session's JWT, if any."""
    return GProspectAPI(token=st.session_state.get("token"))


def _do_login(email: str, password: str) -> None:
    api = GProspectAPI()
    try:
        result = api.login(email, password)
    except APIError as e:
        if e.status_code == 401:
            st.error("Incorrect email or password.")
        elif e.status_code == 403:
            st.error(
                "This account has been deactivated. Contact your administrator."
            )
        elif e.status_code == 429:
            st.error("Too many sign-in attempts. Wait a minute and try again.")
        else:
            st.error(f"Login failed: {e.detail}")
        return

    st.session_state.token = result["access_token"]
    st.session_state.user  = {
        "email":     result["user_email"],
        "full_name": result["full_name"],
        "role":      result["user_role"],
        "org_name":  result["org_name"],
    }
    # Reset wizard state on fresh login — the prior session might have been
    # a different org.
    for key in ("wizard_step", "wizard_payload", "extraction_notes",
                "last_save_error"):
        st.session_state.pop(key, None)

    st.rerun()


def _do_logout() -> None:
    if st.session_state.get("token"):
        _api().logout()
    for key in list(st.session_state.keys()):
        del st.session_state[key]


# ─────────────────────────────────────────────────────────────────────────────
# Login screen
# ─────────────────────────────────────────────────────────────────────────────

def render_login() -> None:
    _, mid, _ = st.columns([1, 2, 1])

    with mid:
        st.title("🎯 GProspect")
        st.caption(
            "Grant prospecting for nonprofits — by AI for Good (P33 Chicago)"
        )
        st.write("")

        with st.form("login_form", clear_on_submit=False):
            email    = st.text_input("Email",   placeholder="admin@deborahsplace.org")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button(
                "Sign in", type="primary", use_container_width=True,
            )

        if submitted:
            if not email.strip() or not password:
                st.warning("Enter both email and password.")
            else:
                _do_login(email.strip().lower(), password)

        st.write("")
        st.caption(f"Backend: `{API_URL}` — set `PORTAL_API_URL` to override.")


# ─────────────────────────────────────────────────────────────────────────────
# Authenticated app
# ─────────────────────────────────────────────────────────────────────────────

def render_sidebar() -> str:
    """Render the sidebar nav. Returns the selected page key."""
    user = st.session_state.user
    with st.sidebar:
        st.markdown(f"### {user['org_name']}")
        st.write(f"Signed in as **{user['full_name']}**")
        st.caption(f"{user['email']} · {user['role']}")
        st.divider()

        # Default page: intake if no profile exists yet, dashboard otherwise.
        if "page" not in st.session_state:
            st.session_state.page = _default_page()

        pages = [
            ("intake",    "📝 Intake"),
            ("prospects", "🎯 Prospects"),
            ("funders",   "🏛️ Funders"),
            ("pipeline",  "📋 Pipeline"),
            ("history",   "🕒 Profile history"),
        ]
        for key, label in pages:
            if st.button(
                label,
                use_container_width = True,
                type                = "primary" if st.session_state.page == key else "secondary",
            ):
                st.session_state.page = key
                st.rerun()

        st.divider()
        if st.button("Sign out", use_container_width=True):
            _do_logout()
            st.rerun()

    return st.session_state.page


def _default_page() -> str:
    """Land on Intake if no profile exists yet; otherwise Prospects."""
    try:
        profile = _api().get_current_profile()
    except APIError:
        return "intake"
    return "prospects" if profile else "intake"


# ─────────────────────────────────────────────────────────────────────────────
# Page renderers
# ─────────────────────────────────────────────────────────────────────────────

def render_dashboard() -> None:
    st.title("🎯 Dashboard")
    st.caption(
        "Phase 1b is up next. Card-based prospect view, pursue/watch/pass "
        "pipeline, deadline urgency badges, and exportable weekly digest "
        "land here. For now: profile status + a basic results summary."
    )

    api = _api()

    # Profile status
    with st.container(border=True):
        st.markdown("**Profile**")
        try:
            current = api.get_current_profile()
        except APIError as e:
            st.error(f"Could not load profile: {e.detail}")
            return

        if current is None:
            st.warning(
                "No profile saved yet. Head to the **Intake** tab to build "
                "one — without a profile, the agent has nothing to match against."
            )
        else:
            payload = current.get("payload") or {}
            cols = st.columns(4)
            cols[0].metric("Version",        current.get("version", "—"))
            cols[1].metric("Mission length", f"{len(payload.get('mission_statement') or '')} chars")
            cols[2].metric("Programs",       len(payload.get("program_areas") or []))
            cols[3].metric("Known funders",  len(payload.get("known_funders") or []))

    # Results placeholder
    with st.container(border=True):
        st.markdown("**Latest results**")
        try:
            summary = api.get_results_summary()
        except APIError as e:
            st.error(f"Could not load results summary: {e.detail}")
            return

        if not summary.get("total_results"):
            st.info(
                "No grant prospects yet. An admin needs to trigger a run "
                "from `POST /results/run` (interactive API docs at "
                f"`{API_URL}/docs`). The first run takes a few minutes."
            )
        else:
            cols = st.columns(3)
            cols[0].metric("Opportunities", summary.get("total_results"))
            cols[1].metric("Top score",     f"{summary.get('top_score'):.2f}"
                           if summary.get('top_score') is not None else "—")
            cols[2].metric("Avg score",     f"{summary.get('avg_score'):.2f}"
                           if summary.get('avg_score') is not None else "—")


def render_history() -> None:
    st.title("🕒 Profile history")
    st.caption(
        "Every saved version of your profile. The current version is what "
        "the agent uses for matching."
    )

    try:
        versions = _api().get_profile_history()
    except APIError as e:
        st.error(f"Could not load history: {e.detail}")
        return

    if not versions:
        st.info("No profile versions saved yet.")
        return

    # Render as a table
    import pandas as pd
    df = pd.DataFrame([
        {
            "Version":    v["version"],
            "Active":     "✅" if v["is_current"] else "",
            "Created at": v["created_at"],
            "Created by user id": v["created_by_user_id"] or "(system seed)",
        }
        for v in versions
    ])
    st.dataframe(df, hide_index=True, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if "token" not in st.session_state:
        render_login()
        return

    page = render_sidebar()
    user = st.session_state.user
    api  = _api()

    if page == "intake":
        render_intake(api, user)
    elif page == "prospects":
        render_prospects(api, user)
    elif page == "funders":
        render_funders(api, user)
    elif page == "pipeline":
        render_pipeline(api, user)
    elif page == "history":
        render_history()
    else:
        st.error(f"Unknown page: {page}")


if __name__ == "__main__":
    main()
