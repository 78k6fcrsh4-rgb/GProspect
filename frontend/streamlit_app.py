"""
frontend/streamlit_app.py
-------------------------
GProspect — Streamlit MVP frontend.

Two screens:
  1. Login (email + password form, hits POST /auth/login on the FastAPI backend)
  2. Dashboard (KPIs + ranked results table + CSV export)

Runs as a separate process on port 8501 and talks to the FastAPI portal over
HTTP. Streamlit makes server-side HTTP requests from Python, so there are no
CORS implications regardless of the portal's CORS config.

Run from the repo root:
    streamlit run frontend/streamlit_app.py
"""

from __future__ import annotations

import os
from typing import Optional

import pandas as pd
import requests
import streamlit as st


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

API_URL = os.getenv("PORTAL_API_URL", "http://localhost:8000").rstrip("/")
REQUEST_TIMEOUT_SECONDS = 30


st.set_page_config(
    page_title = "GProspect",
    page_icon  = "🎯",
    layout     = "wide",
)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _headers(token: Optional[str] = None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def api_post(path: str, **kwargs) -> requests.Response:
    return requests.post(
        f"{API_URL}{path}", timeout=REQUEST_TIMEOUT_SECONDS, **kwargs
    )


def api_get(path: str, token: Optional[str] = None, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.update(_headers(token))
    return requests.get(
        f"{API_URL}{path}",
        headers = headers,
        timeout = REQUEST_TIMEOUT_SECONDS,
        **kwargs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Auth actions
# ─────────────────────────────────────────────────────────────────────────────

def login(email: str, password: str) -> Optional[dict]:
    """
    Hits POST /auth/login with OAuth2 password form fields.
    Returns the token-response dict on success, None on failure (after
    rendering an appropriate error in the UI).
    """
    try:
        resp = api_post(
            "/auth/login",
            data = {
                "username": email,
                "password": password,
            },
        )
    except requests.RequestException as e:
        st.error(
            f"Could not reach the portal at {API_URL}.\n\n"
            f"Make sure the backend is running (`bash ~/WorkBench/AI4GSH/run_portal.sh`).\n\n"
            f"Details: {e}"
        )
        return None

    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 401:
        st.error("Incorrect email or password.")
    elif resp.status_code == 403:
        st.error("This account has been deactivated. Contact your administrator.")
    elif resp.status_code == 429:
        st.error("Too many sign-in attempts. Wait a minute and try again.")
    else:
        st.error(f"Login failed: HTTP {resp.status_code} — {resp.text[:200]}")
    return None


def logout() -> None:
    """Hits POST /auth/logout (best effort) and clears Streamlit session state."""
    token = st.session_state.get("token")
    if token:
        try:
            api_post("/auth/logout", headers=_headers(token))
        except requests.RequestException:
            pass  # best-effort — client side cleanup is what matters
    for key in ("token", "user"):
        st.session_state.pop(key, None)


# ─────────────────────────────────────────────────────────────────────────────
# Login screen
# ─────────────────────────────────────────────────────────────────────────────

def render_login() -> None:
    # Center the form on the page
    _, mid, _ = st.columns([1, 2, 1])

    with mid:
        st.title("🎯 GProspect")
        st.caption(
            "Grant prospecting for nonprofits — by AI for Good (P33 Chicago)"
        )
        st.write("")

        with st.form("login_form", clear_on_submit=False):
            email    = st.text_input(
                "Email", placeholder="admin@deborahsplace.org"
            )
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

        if submitted:
            if not email.strip() or not password:
                st.warning("Enter both email and password.")
                return

            result = login(email.strip().lower(), password)
            if result:
                st.session_state.token = result["access_token"]
                st.session_state.user  = {
                    "email":     result["user_email"],
                    "full_name": result["full_name"],
                    "role":      result["user_role"],
                    "org_name":  result["org_name"],
                }
                st.rerun()

        st.write("")
        st.caption(
            f"Backend: `{API_URL}` — set `PORTAL_API_URL` to override."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

def _handle_401() -> None:
    """Common path when the API rejects the JWT — clear session and rerun."""
    st.warning("Your session has expired. Please sign in again.")
    logout()
    st.rerun()


def render_sidebar(user: dict) -> None:
    with st.sidebar:
        st.markdown(f"### {user['org_name']}")
        st.write(f"Signed in as **{user['full_name']}**")
        st.caption(f"{user['email']} · {user['role']}")
        st.divider()
        if st.button("Sign out", use_container_width=True):
            logout()
            st.rerun()


def render_kpis(summary: dict) -> None:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Opportunities", summary.get("total_results", 0) or 0)
    top = summary.get("top_score")
    avg = summary.get("avg_score")
    col2.metric("Top score",     f"{top:.2f}" if top is not None else "—")
    col3.metric("Average score", f"{avg:.2f}" if avg is not None else "—")
    col4.metric("Last run",      summary.get("last_run") or "—")
    if msg := summary.get("message"):
        st.caption(msg)


def render_dashboard() -> None:
    user  = st.session_state.user
    token = st.session_state.token

    render_sidebar(user)

    st.title("Grant prospects")

    # ---- KPI summary ----
    try:
        resp = api_get("/results/summary", token=token)
    except requests.RequestException as e:
        st.error(f"Could not reach the portal: {e}")
        return

    if resp.status_code == 401:
        _handle_401()
        return
    if resp.status_code != 200:
        st.error(f"Summary endpoint returned {resp.status_code}: {resp.text[:200]}")
        return

    render_kpis(resp.json())
    st.write("")

    # ---- Filters ----
    fcol1, fcol2 = st.columns([1, 4])
    min_score = fcol1.slider(
        "Minimum score",
        min_value = 0.0,
        max_value = 1.0,
        value     = 0.0,
        step      = 0.05,
        help      = "Hide opportunities below this score.",
    )

    # ---- Results table ----
    params = {"limit": 200}
    if min_score > 0:
        params["min_score"] = min_score

    try:
        resp = api_get("/results/", token=token, params=params)
    except requests.RequestException as e:
        st.error(f"Could not load results: {e}")
        return

    if resp.status_code == 401:
        _handle_401()
        return
    if resp.status_code != 200:
        st.error(f"Results endpoint returned {resp.status_code}: {resp.text[:200]}")
        return

    results = resp.json() or []

    if not results:
        st.info(
            "No grant prospects yet.\n\n"
            "An admin needs to trigger a run — either from the CLI "
            "(`python3 run_agent.py --profile profiles/deborah_place.json`) "
            "or by calling `POST /results/run` on the API. The run takes a "
            "few minutes; once it completes, opportunities will appear here."
        )
        return

    df = pd.DataFrame(results)

    # Pick display columns and a sensible order
    display_cols = [
        "rank",
        "funder_name",
        "program_name",
        "score_final",
        "application_deadline",
        "days_remaining",
        "award_range",
        "next_action",
        "application_url",
    ]
    available = [c for c in display_cols if c in df.columns]

    st.dataframe(
        df[available],
        hide_index             = True,
        use_container_width    = True,
        column_config = {
            "rank":                 st.column_config.NumberColumn("#",        width="small"),
            "funder_name":          st.column_config.TextColumn  ("Funder",   width="medium"),
            "program_name":         st.column_config.TextColumn  ("Program",  width="large"),
            "score_final":          st.column_config.NumberColumn("Score",    format="%.2f", width="small"),
            "application_deadline": st.column_config.TextColumn  ("Deadline", width="small"),
            "days_remaining":       st.column_config.NumberColumn("Days left", width="small"),
            "award_range":          st.column_config.TextColumn  ("Award range", width="medium"),
            "next_action":          st.column_config.TextColumn  ("Next action", width="large"),
            "application_url":      st.column_config.LinkColumn  ("Apply",    width="small"),
        },
    )

    st.caption(f"Showing {len(df)} opportunit{'y' if len(df) == 1 else 'ies'}.")
    st.write("")

    # ---- Export ----
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label     = "Download CSV",
        data      = csv_bytes,
        file_name = "grant_prospects.csv",
        mime      = "text/csv",
        type      = "primary",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if "token" not in st.session_state:
        render_login()
    else:
        render_dashboard()


if __name__ == "__main__":
    main()
