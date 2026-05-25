"""
portal/streamlit_app.py
-----------------------
Grant Prospecting Agent — Staff Portal
Built for Deborah's Place by AI for Good / P33 Chicago

Run with: streamlit run portal/streamlit_app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import csv
from datetime import date
from typing import Optional

import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Grant Prospecting — Deborah's Place",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    [data-testid="stSidebar"] { background-color: #1C3C64; }
    [data-testid="stSidebar"] .stMarkdown p { color: white !important; }
    [data-testid="stSidebar"] .stMarkdown h1,
    [data-testid="stSidebar"] .stMarkdown h2,
    [data-testid="stSidebar"] .stMarkdown h3 { color: white !important; }
    [data-testid="stSidebar"] small { color: rgba(255,255,255,0.7) !important; }
    #MainMenu { visibility: hidden; }
    footer    { visibility: hidden; }
    header    { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
defaults = {
    "logged_in": False,
    "user_email": "",
    "user_role": "",
    "user_name": "",
    "current_page": "dashboard",
    "agent_running": False,
    "run_results": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── Helper functions ──────────────────────────────────────────────────────────

def check_login(email: str, password: str) -> Optional[dict]:
    """Verify credentials and return user dict or None."""
    try:
        from sqlalchemy.orm import Session
        from database.db import SessionLocal
        from portal.models.user import User
        from portal.auth.security import verify_password
        db   = SessionLocal()
        user = db.query(User).filter(
            User.email == email.lower().strip()
        ).first()
        db.close()
        if user and verify_password(password, user.hashed_password) and user.is_active:
            return {
                "email":     user.email,
                "full_name": user.full_name,
                "role":      user.role.value,
                "org_name":  user.org_name,
            }
    except Exception as e:
        if "already defined" not in str(e):
            st.error(f"Login error: {e}")
    return None


def load_results() -> list[dict]:
    """Load most recent grant results from outputs folder."""
    try:
        base_dir = ROOT / "outputs" / "deborahs_place"
        if not base_dir.exists():
            return []
        csv_files = sorted(
            base_dir.rglob("grant_prospects_*.csv"), reverse=True
        )
        if not csv_files:
            return []
        results = []
        with open(csv_files[0], "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Map display column names to internal field names
                mapped = {
                    "funder_name":              row.get("Funder Name", ""),
                    "program_name":             row.get("Program Name", ""),
                    "score_final":              row.get("Final Score", ""),
                    "score_composite":          row.get("Composite Score", ""),
                    "application_deadline":     row.get("Application Deadline", ""),
                    "days_remaining":           row.get("Days Remaining", ""),
                    "award_range":              row.get("Award Range", ""),
                    "next_action":              row.get("Recommended Next Action", ""),
                    "is_prior_funder":          row.get("Prior Funder", ""),
                    "geographic_focus":         row.get("Geographic Focus", ""),
                    "eligibility_requirements": row.get("Eligibility Requirements", ""),
                    "application_url":          row.get("Application URL", ""),
                    "score_geographic":         row.get("Score: Geographic Alignment", ""),
                    "score_population":         row.get("Score: Population Alignment", ""),
                    "score_budget":             row.get("Score: Budget Fit", ""),
                    "score_timeline":           row.get("Score: Timeline Feasibility", ""),
                    "reason_geographic":        row.get("Reason: Geographic", ""),
                    "reason_population":        row.get("Reason: Population", ""),
                    "reason_budget":            row.get("Reason: Budget", ""),
                    "reason_timeline":          row.get("Reason: Timeline", ""),
                    "completeness_notes":       row.get("Completeness Notes", ""),
                    "source":                   row.get("Data Source", ""),
                    "date_found":               row.get("Date Found", ""),
                }
                results.append(mapped)
        return results
    except Exception as e:
        st.error(f"Error loading results: {e}")
        return []


def get_urgency(days) -> str:
    """Return urgency level based on days remaining."""
    try:
        d = int(float(str(days)))
        if d <= 30:
            return "hot"
        if d <= 60:
            return "warm"
        return "cool"
    except Exception:
        return "cool"


def to_float(val) -> Optional[float]:
    """Safely convert to float."""
    try:
        return float(val)
    except Exception:
        return None


def safe_progress(val: float) -> float:
    """Clamp progress value to valid range 0.0 to 1.0."""
    return max(0.0, min(1.0, val))


def score_color(val) -> str:
    """Return color string for a score value."""
    s = to_float(val)
    if s is None:
        return "#888888"
    if s >= 4.0:
        return "#27ae60"
    if s >= 2.5:
        return "#d68910"
    return "#c0392b"


def format_award(award: str) -> str:
    """Format award range for safe Streamlit display."""
    if not award or award in ["Not specified", "Amount not specified", ""]:
        return "Amount not specified"
    # Escape dollar signs for Streamlit markdown
    # Streamlit interprets $ as LaTeX math — use unicode dollar sign instead
    award = award.replace("$", "\\$")
    return award


# ── LOGIN PAGE ────────────────────────────────────────────────────────────────

def render_login():
    """Clean login page shown before authentication."""
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown("## 📋 Grant Prospecting Portal")
        st.markdown("**AI for Good — Deborah's Place**")
        st.markdown("Find open, actionable grant opportunities — automatically.")
        st.markdown("---")

        with st.form("login_form"):
            email = st.text_input(
                "Email address",
                placeholder="your@email.com"
            )
            password = st.text_input(
                "Password",
                type="password"
            )
            submitted = st.form_submit_button(
                "Sign In",
                use_container_width=True,
                type="primary"
            )

        if submitted:
            if not email or not password:
                st.error("Please enter your email and password.")
            else:
                with st.spinner("Signing in..."):
                    user = check_login(email, password)
                if user:
                    st.session_state.logged_in = True
                    st.session_state.user_email = user["email"]
                    st.session_state.user_role = user["role"]
                    st.session_state.user_name = user["full_name"]
                    st.session_state.current_page = "dashboard"
                    st.rerun()
                else:
                    st.error("Incorrect email or password. Please try again.")

        st.caption("Need access? Contact your administrator.")


# ── DASHBOARD PAGE ────────────────────────────────────────────────────────────

def render_dashboard():
    """Main dashboard showing ranked grant prospect cards."""

    st.markdown("## 📊 Grant Prospects")
    st.markdown(
        "Ranked opportunities Deborah's Place can act on today. "
        "Updated automatically every morning."
    )

    results = load_results()

    if not results:
        st.info(
            "**No grant results yet.**\n\n"
            "Click **▶️ Run Agent** in the sidebar to search for open grant "
            "opportunities. The first run takes 1 to 3 minutes and will search "
            "the web and grant databases for opportunities that match "
            "Deborah's Place's mission, programs, and location."
        )
        st.markdown("---")
        st.markdown("### How this tool works")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown("**🔍 Step 1 — Run the Agent**")
            st.write(
                "Click Run Agent in the sidebar. The agent searches "
                "the web, filters out grants that do not qualify, and "
                "scores everything that passes."
            )
        with col2:
            st.markdown("**📊 Step 2 — Review Your Results**")
            st.write(
                "Come back to this dashboard. Each grant is shown as "
                "a card with a score, deadline, award range, and a "
                "plain English explanation of why it is a good fit."
            )
        with col3:
            st.markdown("**✅ Step 3 — Take Action**")
            st.write(
                "Each card tells you exactly what to do next. "
                "Click the Apply button to go directly to the "
                "funder's application page."
            )
        return

    # Summary metrics
    hot_count  = sum(1 for r in results if get_urgency(r.get("days_remaining")) == "hot")
    warm_count = sum(1 for r in results if get_urgency(r.get("days_remaining")) == "warm")
    cool_count = sum(1 for r in results if get_urgency(r.get("days_remaining")) == "cool")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Total Opportunities", len(results))
    with c2:
        st.metric("🔴 Act Now", hot_count, help="Deadline within 30 days")
    with c3:
        st.metric("🟠 Coming Up", warm_count, help="Deadline in 31-60 days")
    with c4:
        st.metric("🔵 On Radar", cool_count, help="Deadline beyond 60 days")

    st.markdown("---")

    # Score guide
    with st.expander("📖 How are grants scored? Click to read the guide."):
        st.markdown("""
The agent scores each grant using AI across **four criteria** on a 1 to 5 scale.
Budget fit counts **double** because it is the most important factor.

| Score | What it means |
|---|---|
| 4.5 – 5.0 | Excellent match — apply immediately |
| 3.5 – 4.4 | Strong match — high priority |
| 2.5 – 3.4 | Good match — worth pursuing |
| 1.5 – 2.4 | Partial match — review carefully before applying |
| 1.0 – 1.4 | Weak match — low priority |

**The four criteria:**
- 📍 **Geographic Alignment** — does this grant specifically target Chicago?
- 👥 **Population Alignment** — does it serve women experiencing homelessness?
- 💰 **Budget Fit** *(counts double)* — does the award range match our grant request range?
- 📅 **Timeline Feasibility** — is the deadline realistic for preparation?
        """)

    # Filter
    filter_by = st.selectbox(
        "Filter by urgency",
        ["All opportunities", "🔴 Act Now (deadline within 30 days)",
         "🟠 Coming Up (31 to 60 days)", "🔵 On Radar (60+ days)"]
    )

    filtered = results
    if "Act Now" in filter_by:
        filtered = [r for r in results if get_urgency(r.get("days_remaining")) == "hot"]
    elif "Coming Up" in filter_by:
        filtered = [r for r in results if get_urgency(r.get("days_remaining")) == "warm"]
    elif "On Radar" in filter_by:
        filtered = [r for r in results if get_urgency(r.get("days_remaining")) == "cool"]

    st.markdown(f"**Showing {len(filtered)} of {len(results)} opportunities**")
    st.markdown("---")

    if not filtered:
        st.info("No opportunities match this filter. Try a different urgency level.")
        return

    # Grant cards
    for idx, r in enumerate(filtered):
        u       = get_urgency(r.get("days_remaining"))
        funder  = r.get("funder_name", "Unknown Funder")
        program = r.get("program_name", "")
        dead    = r.get("application_deadline", "Not listed")
        days    = r.get("days_remaining", "")
        award   = format_award(r.get("award_range", ""))
        action  = r.get("next_action", "")
        url     = r.get("application_url", "")
        score   = r.get("score_final", "")
        prior   = r.get("is_prior_funder", "")
        elig    = r.get("eligibility_requirements", "")
        notes   = r.get("completeness_notes", "")
        geo_s   = r.get("score_geographic", "")
        pop_s   = r.get("score_population", "")
        bud_s   = r.get("score_budget", "")
        tim_s   = r.get("score_timeline", "")
        geo_r   = r.get("reason_geographic", "")
        pop_r   = r.get("reason_population", "")
        bud_r   = r.get("reason_budget", "")
        tim_r   = r.get("reason_timeline", "")

        # Urgency setup
        urgency_labels = {
            "hot":  "🔴 Act Now — deadline within 30 days",
            "warm": "🟠 Coming Up — deadline in 31 to 60 days",
            "cool": "🔵 On Radar — deadline beyond 60 days",
        }
        ulabel = urgency_labels.get(u, "🔵 On Radar")

        # Score setup — cap at 5.0 for display, cap progress at 1.0
        sf = to_float(score)
        if sf is not None:
            sf_display = min(sf, 5.0)  # display max 5.0
        else:
            sf_display = None
        sc = score_color(score)
        score_display = f"{sf:.2f} / 5.00" if sf else "Not scored"
        score_pct = safe_progress((sf / 5.0)) if sf else 0.0
        stars = "⭐" * min(round(sf), 5) if sf else ""

        # Prior funder tag
        prior_tag = " — ⭐ Prior Funder" if "warm lead" in str(prior).lower() else ""

        # Days display
        try:
            days_int = int(float(str(days)))
            days_str = f"{days_int} days remaining"
        except Exception:
            days_str = ""

        # Render card
        st.markdown(f"### {funder}{prior_tag}")
        st.markdown(f"*{program}*")
        st.markdown(f"**{ulabel}**")

        col_left, col_right = st.columns([3, 1])

        with col_left:
            st.markdown(f"**📅 Deadline:** {dead}" + (f" — {days_str}" if days_str else ""))
            if award == "Amount not specified":
                st.warning("💰 **Award amount unknown** — verify directly with the funder")
            else:
                st.markdown(f"**💰 Award Range:** {award}")

        with col_right:
            st.markdown(f"**Score: {score_display}**")
            if score_pct > 0:
                st.progress(score_pct)
            if stars:
                st.markdown(stars)

        # Next action
        if action:
            if u == "hot":
                st.error(f"**What to do next:** {action}")
            elif u == "warm":
                st.warning(f"**What to do next:** {action}")
            else:
                st.info(f"**What to do next:** {action}")

        # Full details expander
        with st.expander("See full details — eligibility, scores, and apply link"):

            if elig and elig not in ["Not specified", ""]:
                st.markdown("**Eligibility Requirements:**")
                st.write(elig)
                st.markdown("---")

            st.markdown("**Score Breakdown — why did it get this score?**")

            criteria = [
                ("📍 Geographic Alignment", geo_s, geo_r),
                ("👥 Population Alignment",  pop_s, pop_r),
                ("💰 Budget Fit (counts double)", bud_s, bud_r),
                ("📅 Timeline Feasibility",  tim_s, tim_r),
            ]

            for label, s, reason in criteria:
                sv = to_float(s)
                if sv is not None:
                    col_a, col_b = st.columns([1, 3])
                    with col_a:
                        st.markdown(f"**{sv:.0f} / 5**")
                        st.markdown(f"{label}")
                    with col_b:
                        st.progress(safe_progress(sv / 5.0))
                        if reason and reason not in ["Not available", ""]:
                            st.caption(reason)

            if notes and notes not in ["Complete", ""]:
                st.warning(f"⚠️ Still needs research before applying: {notes}")

            st.markdown("---")

            if url and url not in ["Not found", ""]:
                st.link_button(
                    "🔗 Go to Application Page",
                    url,
                    use_container_width=True,
                    type="primary"
                )
            else:
                st.info(
                    "Application URL was not found automatically. "
                    "Search the funder's website directly to find the application page."
                )

        st.markdown("---")


# ── RUN AGENT PAGE ────────────────────────────────────────────────────────────

def render_run_agent():
    """Run Agent page — trigger a new search."""

    st.markdown("## ▶️ Run Grant Agent")
    st.markdown(
        "Click the button below to search for new grant opportunities right now. "
        "The agent will search the web and grant databases, filter out anything "
        "that does not qualify, score each opportunity with AI, and update "
        "your dashboard with everything it finds."
    )

    # Stats from last run
    try:
        from agent.profile import OrgProfile
        from agent.state import AgentState
        profile  = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
        state    = AgentState(profile)
        last_run = state.get_last_run()
        stats    = state.get_stats()

        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Total Runs Completed", stats.get("total_runs", 0))
        with c2:
            st.metric("Sources Being Monitored", stats.get("watch_list_size", 0))
        with c3:
            lr = last_run["timestamp"][:10] if last_run else "Never"
            st.metric("Last Run Date", lr)
    except Exception:
        pass

    st.markdown("---")
    st.markdown("### Search Options")

    search_type = st.radio(
        "How thorough should this search be?",
        [
            "Quick search — 5 queries, about 1 minute",
            "Standard search — 10 queries, about 2 minutes",
            "Deep search — 20 queries, about 4 minutes",
            "Search for a specific funder by name",
        ]
    )

    custom_search = ""
    if "specific funder" in search_type:
        custom_search = st.text_input(
            "Enter the funder name to search for",
            placeholder="e.g. Polk Bros Foundation open grants 2026"
        )

    query_map = {
        "Quick search — 5 queries, about 1 minute": 5,
        "Standard search — 10 queries, about 2 minutes": 10,
        "Deep search — 20 queries, about 4 minutes": 20,
        "Search for a specific funder by name": 1,
    }
    max_q = query_map.get(search_type, 5)

    st.markdown("---")

    # Run button
    if st.session_state.agent_running:
        st.button("⏳ Agent is running...", disabled=True, use_container_width=True)
        st.info("The agent is searching for grants right now. This takes 1 to 3 minutes. Please wait.")
    else:
        if st.button("▶️ Run Agent Now", type="primary", use_container_width=True):
            st.session_state.agent_running = True
            with st.spinner(
                "Searching for open grants for Deborah's Place... "
                "checking foundation websites, grant databases, and the web..."
            ):
                try:
                    from agent.profile import OrgProfile
                    from agent.loop import AgentLoop
                    from output.formatter import ResultFormatter
                    from output.exporter import ResultExporter

                    profile   = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
                    loop      = AgentLoop(profile)
                    formatter = ResultFormatter(profile)
                    exporter  = ResultExporter(profile)

                    results = loop.run(
                        max_queries=max_q,
                        custom_queries=[custom_search] if custom_search else None,
                    )

                    if results:
                        formatted = formatter.format_all(results)
                        exporter.export_csv(formatted)
                        exporter.export_excel(formatted)
                        exporter.export_run_summary(formatted)

                    st.session_state.run_results   = len(results)
                    st.session_state.agent_running = False

                except Exception as e:
                    st.session_state.agent_running = False
                    st.error(f"The agent encountered an error: {e}")
                    st.session_state.run_results = None

            st.rerun()

    # Results after run
    if st.session_state.run_results is not None:
        count = st.session_state.run_results
        if count > 0:
            st.success(
                f"✅ **Run complete!**\n\n"
                f"The agent found **{count} grant "
                f"{'opportunity' if count == 1 else 'opportunities'}** "
                f"for Deborah's Place.\n\n"
                f"Click **📊 Dashboard** in the sidebar to see the full ranked list "
                f"with scores, deadlines, and next steps for each one."
            )
        else:
            st.info(
                "✅ **Run complete.**\n\n"
                "No new opportunities were found matching Deborah's Place's "
                "criteria in this search. This can happen when all results "
                "were federal grants (which are filtered out) or when deadlines "
                "fall outside the acceptable range.\n\n"
                "Try a deeper search or search for a specific funder by name."
            )

        if st.button("Clear this message"):
            st.session_state.run_results = None
            st.rerun()


# ── MANUAL INPUT PAGE ─────────────────────────────────────────────────────────

def render_manual_input():
    """Submit a grant found by staff."""

    st.markdown("## ✉️ Submit a Grant You Found")
    st.markdown(
        "Found a grant opportunity the agent did not surface? "
        "Submit it here. The agent will score it against Deborah's Place's profile, "
        "add it to your results, and learn from it so it finds similar "
        "opportunities automatically in the future."
    )

    with st.form("manual_input_form"):

        st.markdown("### How did you find this grant?")

        how_heard = st.selectbox(
            "How did you hear about this opportunity?",
            ["Website", "Email newsletter", "In-person or conference",
             "Partner organization shared it", "Social media", "Other"]
        )

        source_name = st.text_input(
            "Name of the source",
            placeholder="e.g. Philanthropy News Digest, MacArthur Foundation website"
        )

        st.markdown("---")
        st.markdown("### Grant Details")

        funder_name  = st.text_input(
            "Funding organization (required)",
            placeholder="e.g. Polk Bros. Foundation"
        )
        program_name = st.text_input(
            "Grant program name (required)",
            placeholder="e.g. Community Impact Grant 2026"
        )

        app_url = ""
        if how_heard == "Website":
            app_url = st.text_input(
                "Application URL",
                placeholder="https://www.foundationname.org/apply"
            )

        st.markdown("---")
        st.markdown("### Award and Deadline")

        award_range = st.selectbox(
            "Estimated award range",
            [
                "Not sure",
                "Under $10,000",
                "$10,000 to $25,000",
                "$25,000 to $50,000",
                "$50,000 to $100,000",
                "$100,000 to $250,000",
                "$250,000 to $500,000",
                "Over $500,000",
            ]
        )

        deadline_known = st.checkbox("I know the application deadline date")
        deadline_val   = None
        if deadline_known:
            deadline_val = st.date_input(
                "Application deadline",
                min_value=date.today()
            )
        else:
            st.caption("Leave unchecked if the deadline is unknown or rolling.")

        st.markdown("---")
        st.markdown("### Additional Information")

        eligibility = st.text_area(
            "Eligibility requirements if known",
            height=80,
            placeholder="e.g. Chicago nonprofits serving women experiencing homelessness"
        )

        description = st.text_area(
            "Description — what does this grant fund?",
            height=80,
            placeholder="Brief description of what this grant supports"
        )

        submitted_by = st.text_input(
            "Your name",
            value=st.session_state.user_name
        )

        submit_btn = st.form_submit_button(
            "Submit and Score This Grant",
            type="primary",
            use_container_width=True
        )

    if submit_btn:
        if not funder_name.strip() or not program_name.strip():
            st.error("Please enter the funding organization name and grant program name.")
        else:
            with st.spinner("Submitting and scoring... about 30 seconds."):
                try:
                    from agent.profile import OrgProfile
                    from learning.feedback import FeedbackProcessor

                    profile   = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
                    processor = FeedbackProcessor(profile)

                    source_url = app_url if app_url else f"Submitted manually via {how_heard}"

                    result = processor.submit(
                        funder_name    = funder_name.strip(),
                        program_name   = program_name.strip(),
                        source_url     = source_url,
                        submitted_by   = submitted_by,
                        deadline       = str(deadline_val) if deadline_val else None,
                        award_range    = award_range if award_range != "Not sure" else None,
                        eligibility    = eligibility.strip() or None,
                        notes          = f"Heard via: {how_heard} — {source_name}. {description}",
                        funder_website = app_url or None,
                    )

                    if result["success"]:
                        if result["already_found"]:
                            st.info("✅ The agent had already found this opportunity. You can see it on your dashboard.")
                        else:
                            st.success(f"✅ **Submitted successfully!**\n\n{result['message']}")
                            if result.get("learned"):
                                st.info(f"**What the agent learned:**\n\n{result['learned']}")
                    else:
                        st.error(result["message"])

                except Exception as e:
                    st.error(f"Submission error: {e}")


# ── ADMIN PAGE ────────────────────────────────────────────────────────────────

def render_admin():
    """Admin page — users, watch list, learning log, settings."""

    if st.session_state.user_role != "admin":
        st.error("Administrator access is required to view this page.")
        return

    st.markdown("## ⚙️ Admin Dashboard")

    tab1, tab2, tab3, tab4 = st.tabs([
        "👥 Users", "📡 Watch List", "🧠 Learning Log", "⚙️ Settings"
    ])

    with tab1:
        st.markdown("### User Accounts")
        try:
            from database.db import SessionLocal
            from portal.models.user import User
            db    = SessionLocal()
            users = db.query(User).filter(User.org_name == "Deborah's Place").all()
            db.close()
            if users:
                for u in users:
                    c1, c2, c3 = st.columns([3, 1, 1])
                    with c1:
                        role_tag = "👑 ADMIN" if u.role.value == "admin" else "👤 USER"
                        st.write(f"**{u.full_name}** {role_tag} — {u.email}")
                    with c2:
                        st.write("✅ Active" if u.is_active else "❌ Inactive")
                    with c3:
                        lr = u.last_login.strftime("%m/%d/%Y") if u.last_login else "Never"
                        st.caption(f"Last login: {lr}")
            else:
                st.info("No users found.")
        except Exception as e:
            st.error(f"Could not load users: {e}")

        st.markdown("---")
        st.markdown("### Create a New User")
        with st.form("create_user_form"):
            new_email    = st.text_input("Email address")
            new_name     = st.text_input("Full name")
            new_password = st.text_input("Temporary password", type="password")
            new_role     = st.selectbox("Role", ["user", "admin"])
            create_btn   = st.form_submit_button("Create User", type="primary")

        if create_btn:
            if not new_email or not new_name or not new_password:
                st.error("Please fill in all three fields.")
            else:
                try:
                    from database.db import SessionLocal
                    from portal.models.user import User, UserRole
                    from portal.auth.security import hash_password
                    db = SessionLocal()
                    existing = db.query(User).filter(User.email == new_email.lower()).first()
                    if existing:
                        st.error(f"Email {new_email} is already registered.")
                    else:
                        db.add(User(
                            email=new_email.lower().strip(), full_name=new_name,
                            org_name="Deborah's Place",
                            hashed_password=hash_password(new_password),
                            role=UserRole(new_role), is_active=True, is_verified=True,
                        ))
                        db.commit()
                        db.close()
                        st.success(f"✅ User {new_email} created.")
                        st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    with tab2:
        st.markdown("### Sources the Agent Monitors")
        try:
            from agent.profile import OrgProfile
            from agent.state import AgentState
            profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
            state   = AgentState(profile)
            sources = state.get_watch_list()
            high   = [s for s in sources if s.get("priority") == "high"]
            medium = [s for s in sources if s.get("priority") == "medium"]
            other  = [s for s in sources if s.get("priority") not in ["high","medium"]]
            st.write(f"**{len(sources)} sources** — {len(high)} high, {len(medium)} medium priority")
            st.markdown("---")
            for group_label, group in [("🔴 High Priority", high), ("🟠 Medium Priority", medium), ("🔵 Other", other)]:
                if group:
                    st.markdown(f"**{group_label}**")
                    for s in group:
                        c1, c2 = st.columns([4, 1])
                        with c1:
                            st.write(f"• **{s['name']}**")
                            st.caption(s["url"])
                        with c2:
                            st.caption(f"By: {s.get('added_by','seed')}")
        except Exception as e:
            st.error(f"Could not load watch list: {e}")

        st.markdown("---")
        st.markdown("### Add a New Source")
        with st.form("add_source_form"):
            src_name = st.text_input("Foundation name")
            src_url  = st.text_input("URL", placeholder="https://www.foundation.org/grants")
            src_pri  = st.selectbox("Priority", ["high", "medium", "low"])
            add_btn  = st.form_submit_button("Add to Watch List", type="primary")
        if add_btn:
            if not src_name or not src_url:
                st.error("Name and URL required.")
            else:
                try:
                    from agent.profile import OrgProfile
                    from agent.state import AgentState
                    profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
                    state   = AgentState(profile)
                    added   = state.add_to_watch_list(name=src_name, url=src_url, priority=src_pri,
                                added_by=f"admin:{st.session_state.user_email}")
                    state.save()
                    st.success(f"✅ Added {src_name}." if added else f"{src_name} already in list.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    with tab3:
        st.markdown("### What the Agent Has Learned")
        try:
            from agent.profile import OrgProfile
            from learning.learning_log import LearningLog
            profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
            log     = LearningLog(profile)
            stats   = log.get_stats()
            entries = log.get_recent_changes(limit=20)
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("Submissions", stats.get("submissions", 0))
            with c2: st.metric("Agent Updates", stats.get("changes", 0))
            with c3: st.metric("Sources Added", stats.get("sources_added", 0))
            st.markdown("---")
            if entries:
                for entry in entries[:10]:
                    t  = entry.get("timestamp","")[:10]
                    et = entry.get("entry_type","")
                    if et == "submission":
                        sub = entry.get("submission", {})
                        st.info(f"📥 **{t}** — {sub.get('funder_name','?')} — {sub.get('program_name','')}")
                        if entry.get("learned"):
                            st.caption(entry["learned"][:200])
                    elif et == "change":
                        st.success(f"🔧 **{t}** — {entry.get('description','')[:150]}")
            else:
                st.info("No learning log entries yet.")
        except Exception as e:
            st.error(f"Could not load learning log: {e}")

    with tab4:
        st.markdown("### Current Agent Settings")
        try:
            from agent.profile import OrgProfile
            profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
            st.markdown(f"""
| Setting | Value |
|---|---|
| Organization | {profile.org_name} |
| Location | {profile.geography.city}, {profile.geography.state} |
| Grant range | ${profile.budget.request_floor:,} – ${profile.budget.request_ceiling:,} |
| Federal grants excluded | {"✅ Yes" if profile.settings.exclude_federal else "❌ No"} |
| Min days until deadline | {profile.settings.deadline_floor_days} days |
| Max days until deadline | {profile.settings.deadline_ceiling_days} days |
| Min score to show | {profile.settings.min_composite_score} / 5.0 |
| Active programs | {len(profile.program_areas)} |
| Known funders | {len(profile.known_funders)} |
            """)
            st.markdown("**Known Prior Funders:**")
            for f in profile.known_funders:
                st.write(f"• {f.name} — last award {f.last_award_year}")
        except Exception as e:
            st.error(f"Could not load settings: {e}")


# ── SIDEBAR ───────────────────────────────────────────────────────────────────

def render_sidebar():
    """Sidebar navigation with user info and logout."""
    with st.sidebar:
        st.markdown("##### MENU")
        st.markdown("### 📋 Grant Prospecting")
        st.caption("AI for Good — Deborah's Place")
        st.markdown("---")

        if st.session_state.logged_in:
            role_icon = "👑" if st.session_state.user_role == "admin" else "👤"
            st.markdown(f"**{role_icon} {st.session_state.user_name}**")
            st.caption(st.session_state.user_email)
            st.caption(f"Signed in as: {st.session_state.user_role.upper()}")
            st.markdown("---")

        nav_pages = [
            ("📊 Dashboard",      "dashboard"),
            ("▶️ Run Agent",       "run_agent"),
            ("✉️ Submit a Grant",  "manual_input"),
        ]
        if st.session_state.user_role == "admin":
            nav_pages.append(("⚙️ Admin", "admin"))

        for label, key in nav_pages:
            is_active = st.session_state.current_page == key
            if st.button(label, key=f"nav_{key}", use_container_width=True,
                         type="primary" if is_active else "secondary"):
                st.session_state.current_page = key
                st.rerun()

        st.markdown("---")
        results = load_results()
        if results:
            try:
                base_dir  = ROOT / "outputs" / "deborahs_place"
                csv_files = sorted(base_dir.rglob("grant_prospects_*.csv"), reverse=True)
                if csv_files:
                    with open(csv_files[0], "r") as f:
                        csv_data = f.read()
                    st.download_button(
                        label="⬇️ Download Results (CSV)",
                        data=csv_data,
                        file_name=f"grant_prospects_{date.today()}.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )
            except Exception:
                pass

        st.markdown("---")
        st.markdown(" ")
        if st.button("🚪 Sign Out", use_container_width=True):
            for key in list(defaults.keys()):
                st.session_state[key] = defaults[key]
            st.rerun()


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    if not st.session_state.logged_in:
        render_login()
        return

    render_sidebar()

    page = st.session_state.current_page
    if page == "dashboard":
        render_dashboard()
    elif page == "run_agent":
        render_run_agent()
    elif page == "manual_input":
        render_manual_input()
    elif page == "admin":
        render_admin()
    else:
        render_dashboard()


if __name__ == "__main__":
    main()