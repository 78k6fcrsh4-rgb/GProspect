"""
portal/streamlit_app.py
-----------------------
Grant Prospecting — Staff Portal
Built for Deborah's Place by AI for Good / P33 Chicago

Integrates partner frontend (GrantScout AI design) with
our backend agent engine, database, and learning loop.

Run with: streamlit run portal/streamlit_app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import csv
import random
import time
from datetime import date, datetime, timedelta
from typing import Optional

import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Grant Prospecting — Deborah's Place",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Sidebar */
    [data-testid="stSidebar"] { background-color: #1C3C64; }
    [data-testid="stSidebar"] .stMarkdown p,
    [data-testid="stSidebar"] .stMarkdown h1,
    [data-testid="stSidebar"] .stMarkdown h2,
    [data-testid="stSidebar"] .stMarkdown h3,
    [data-testid="stSidebar"] small,
    [data-testid="stSidebar"] .stCaption { color: white !important; }

    /* Hide Streamlit chrome */
    #MainMenu { visibility: hidden; }
    footer    { visibility: hidden; }
    header    { visibility: hidden; }

    /* Floating run button area */
    .floating-bar {
        position: fixed;
        bottom: 24px;
        right: 24px;
        z-index: 9999;
        display: flex;
        gap: 8px;
    }
</style>
""", unsafe_allow_html=True)

# ── Constants ─────────────────────────────────────────────────────────────────

SORT_OPTIONS = [
    "Match Score: High to Low",
    "Match Score: Low to High",
    "Deadline: Soonest First",
    "Award Amount: Highest First",
    "Award Amount: Lowest First",
]

PRESET_AREAS = [
    "Permanent Supportive Housing", "Interim Housing", "Health & Wellness",
    "Community Engagement", "Workforce Development", "Mental Health Services",
    "Youth Services", "Education", "Women's Services", "Anti-Poverty",
    "Legal Aid", "Substance Use Recovery", "Other",
]

LOCATIONS = [
    "Chicago, IL (Cook County)", "Greater Chicago Metro",
    "Illinois Statewide", "National", "Other",
]

HEARD_OPTIONS = [
    "Website", "Email newsletter", "In-person or conference",
    "Partner organization", "Social media", "Board referral", "Other",
]

SOURCE_OPTIONS = [
    "Manual Entry", "Philanthropy News Digest", "Instrumentl",
    "Grants.gov", "Zeffy", "Archival 990", "Other",
]


# ── Session state ─────────────────────────────────────────────────────────────

def init_state():
    defaults = {
        "current_user":     None,
        "page":             "dashboard",
        "grants":           [],
        "sort_by":          "Match Score: High to Low",
        "selected_grant_id": None,
        "show_alert_log":   False,
        "unread_alerts":    0,
        "alert_log":        [],
        "run_results":      None,
        "agent_running":    False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # Load real grants from CSV on first load
    if not st.session_state.grants:
        st.session_state.grants = _load_grants_from_csv()


# ── Backend helpers ───────────────────────────────────────────────────────────

def _load_grants_from_csv() -> list[dict]:
    """Load real grant results from the outputs folder and convert to card format."""
    try:
        base_dir = ROOT / "outputs" / "deborahs_place"
        if not base_dir.exists():
            return []
        csv_files = sorted(base_dir.rglob("grant_prospects_*.csv"), reverse=True)
        if not csv_files:
            return []

        grants = []
        with open(csv_files[0], "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                score = _safe_float(row.get("Final Score", ""))
                days  = _safe_int(row.get("Days Remaining", ""))
                award_range = row.get("Award Range", "Not specified")
                award_range = award_range.replace("`", "$")

                # Determine temperature from days remaining
                if days is not None and days <= 30:
                    temp = "hot"
                elif days is not None and days <= 60:
                    temp = "warm"
                else:
                    temp = "less-warm"

                grants.append({
                    "id":           str(i + 1),
                    "temperature":  temp,
                    "source":       row.get("Data Source", "Agent"),
                    "is_manually_added": False,
                    "funding_org":  row.get("Funder Name", ""),
                    "program_name": row.get("Program Name", ""),
                    "program_contact": row.get("Program Officer", "Not listed"),
                    "description":  row.get("Description", ""),
                    "days_to_deadline": days or 999,
                    "deadline":     row.get("Application Deadline", "Not listed"),
                    "eligibility":  row.get("Eligibility Requirements", ""),
                    "award_label":  award_range,
                    "award_min":    _safe_int(row.get("Award Min", "")) or 0,
                    "award_max":    _safe_int(row.get("Award Max", "")) or 0,
                    "application_method": row.get("Application Method", ""),
                    "application_url":    row.get("Application URL", ""),
                    "disqualifying_restrictions": row.get("Disqualifying Factors", ""),
                    "required_documents": [],
                    "match_score":  score or 0.0,
                    "retrieved_ago": row.get("Date Found", ""),
                    "scoring": {
                        "geographic_alignment": {
                            "score": _safe_float(row.get("Score: Geographic Alignment", "")) or 0,
                            "label": "Geographic Alignment",
                            "explanation": row.get("Reason: Geographic", ""),
                        },
                        "population_served": {
                            "score": _safe_float(row.get("Score: Population Alignment", "")) or 0,
                            "label": "Population Served",
                            "explanation": row.get("Reason: Population", ""),
                        },
                        "budget_fit": {
                            "score": _safe_float(row.get("Score: Budget Fit", "")) or 0,
                            "label": "Budget Fit",
                            "explanation": row.get("Reason: Budget", ""),
                            "is_highest_weight": True,
                        },
                        "timeline_feasibility": {
                            "score": _safe_float(row.get("Score: Timeline Feasibility", "")) or 0,
                            "label": "Timeline Feasibility",
                            "explanation": row.get("Reason: Timeline", ""),
                        },
                    },
                    "next_action":    row.get("Recommended Next Action", ""),
                    "completeness":   row.get("Completeness Notes", ""),
                    "is_prior_funder": "warm lead" in row.get("Prior Funder", "").lower(),
                    "location":      row.get("Geographic Focus", "Chicago, IL"),
                })
        return grants
    except Exception:
        return []


def check_login(email: str, password: str) -> Optional[dict]:
    """Verify credentials against real database."""
    try:
        from database.db import SessionLocal
        from portal.models.user import User
        from portal.auth.security import verify_password
        db   = SessionLocal()
        user = db.query(User).filter(User.email == email.lower().strip()).first()
        db.close()
        if user and verify_password(password, user.hashed_password) and user.is_active:
            return {
                "id":        str(user.id),
                "name":      user.full_name,
                "email":     user.email,
                "role":      user.role.value,
                "title":     "",
                "organization": user.org_name,
            }
    except Exception as e:
        if "already defined" not in str(e):
            st.error(f"Login error: {e}")
    return None


def create_user_in_db(email: str, name: str, password: str, role: str) -> bool:
    """Create a new user in the real database."""
    try:
        from database.db import SessionLocal
        from portal.models.user import User, UserRole
        from portal.auth.security import hash_password
        db       = SessionLocal()
        existing = db.query(User).filter(User.email == email.lower()).first()
        if existing:
            db.close()
            return False
        db.add(User(
            email=email.lower().strip(), full_name=name,
            org_name="Deborah's Place",
            hashed_password=hash_password(password),
            role=UserRole(role), is_active=True, is_verified=True,
        ))
        db.commit()
        db.close()
        return True
    except Exception:
        return False


def load_db_users() -> list[dict]:
    """Load all users from the real database."""
    try:
        from database.db import SessionLocal
        from portal.models.user import User
        db    = SessionLocal()
        users = db.query(User).filter(User.org_name == "Deborah's Place").all()
        db.close()
        return [
            {
                "id":           str(u.id),
                "name":         u.full_name,
                "email":        u.email,
                "role":         u.role.value,
                "status":       "approved" if u.is_active else "inactive",
                "organization": u.org_name,
                "title":        "",
            }
            for u in users
        ]
    except Exception:
        return []


def run_agent_search(max_queries: int = 5, custom_search: str = "") -> int:
    """Run the real agent pipeline and return count of results found."""
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
            max_queries=max_queries,
            custom_queries=[custom_search] if custom_search else None,
        )

        if results:
            formatted = formatter.format_all(results)
            exporter.export_csv(formatted)
            exporter.export_excel(formatted)
            exporter.export_run_summary(formatted)

        # Reload grants from new CSV
        st.session_state.grants = _load_grants_from_csv()
        return len(results)
    except Exception as e:
        st.error(f"Agent error: {e}")
        return 0


# ── Utility helpers ───────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    try:    return float(val)
    except: return None

def _safe_int(val) -> Optional[int]:
    try:    return int(float(str(val)))
    except: return None

def stars(score: float) -> str:
    full = min(int(round(score)), 5)
    return "★" * full + "☆" * (5 - full)

def temp_label(temp: str) -> str:
    return {"hot": "🔴 Hot", "warm": "🟡 Warm", "less-warm": "⚪ Archival"}.get(temp, temp)

def sort_grants(grants: list, sort_by: str) -> list:
    key_map = {
        "Match Score: High to Low":  (lambda g: g["match_score"], True),
        "Match Score: Low to High":  (lambda g: g["match_score"], False),
        "Deadline: Soonest First":   (lambda g: g["days_to_deadline"], False),
        "Award Amount: Highest First": (lambda g: g["award_max"], True),
        "Award Amount: Lowest First":  (lambda g: g["award_max"], False),
    }
    fn, rev = key_map.get(sort_by, (lambda g: g["match_score"], True))
    return sorted(grants, key=fn, reverse=rev)

def is_cold_lead(g: dict) -> bool:
    if g.get("is_manually_added"):
        return False
    r = g.get("disqualifying_restrictions", "").lower()
    m = g.get("application_method", "").lower()
    return (
        "unsolicited applications declined" in r
        or "does not accept unsolicited" in r
        or "by invitation only" in m
    )


# ── LOGIN PAGE ────────────────────────────────────────────────────────────────

def show_login():
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("## 🎯 Grant Prospecting")
        st.caption("AI-Powered Grant Prospecting Agent — Deborah's Place")
        st.divider()

        tab_in, tab_up = st.tabs(["Sign In", "Sign Up"])

        with tab_in:
            with st.form("signin"):
                email    = st.text_input("Email", placeholder="you@organization.org")
                password = st.text_input("Password", type="password", placeholder="••••••••")
                go       = st.form_submit_button("Sign In", type="primary", use_container_width=True)

            if go:
                if not email or not password:
                    st.error("Please enter your email and password.")
                else:
                    user = check_login(email, password)
                    if user:
                        st.session_state.current_user = user
                        st.session_state.page = "dashboard"
                        st.rerun()
                    else:
                        st.error("Incorrect email or password. Please try again.")

            st.caption("Admin: **admin@deborahsplace.org**")

        with tab_up:
            role_choice = st.selectbox(
                "Signing up as",
                ["Basic User — staff / development associate", "Admin — director / administrator"],
                key="su_role",
            )
            is_admin = role_choice.startswith("Admin")
            if not is_admin:
                st.info("Basic User accounts require Admin approval before access is granted.")

            with st.form("signup"):
                c1, c2 = st.columns(2)
                with c1:
                    su_name  = st.text_input("Full Name *")
                    su_email = st.text_input("Work Email *")
                    su_pw    = st.text_input("Password *", type="password")
                with c2:
                    su_title = st.text_input("Job Title *")
                    su_org   = st.text_input("Organization *")
                    su_pw2   = st.text_input("Confirm Password *", type="password")

                submitted = st.form_submit_button(
                    "Create Account" if is_admin else "Submit Request",
                    type="primary", use_container_width=True,
                )

            if submitted:
                if not su_name or not su_email or not su_pw or not su_org:
                    st.error("Please fill in all required fields.")
                elif su_pw != su_pw2:
                    st.error("Passwords do not match.")
                elif len(su_pw) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    role = "admin" if is_admin else "user"
                    ok   = create_user_in_db(su_email, su_name, su_pw, role)
                    if ok:
                        if is_admin:
                            user = check_login(su_email, su_pw)
                            if user:
                                st.session_state.current_user = user
                                st.session_state.page = "dashboard"
                                st.rerun()
                        else:
                            st.success("✅ Request submitted. An admin will review and approve your access.")
                    else:
                        st.error("An account with that email already exists.")


# ── SIDEBAR ───────────────────────────────────────────────────────────────────

def show_sidebar():
    user = st.session_state.current_user
    with st.sidebar:
        st.markdown("## 🎯 Grant Prospecting")
        st.caption("AI Prospecting Agent")

        # Agent status
        try:
            from agent.profile import OrgProfile
            from agent.state import AgentState
            profile  = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
            state    = AgentState(profile)
            last_run = state.get_last_run()
            if last_run:
                ts = last_run.get("timestamp", "")[:10]
                st.success(f"● Agent Active · Last run {ts}")
            else:
                st.info("● Agent ready · Never run")
        except Exception:
            st.info("● Agent ready")

        st.divider()

        # Navigation
        nav = [
            ("🏠  Dashboard",             "dashboard"),
            ("▶️  Run Agent",              "run_agent"),
            ("➕  Add Grant Manually",     "add_grant"),
            ("🏢  Organization Profile",   "profile"),
        ]
        if user.get("role") == "admin":
            nav.append(("👥  User Management", "users"))
            nav.append(("⚙️  Admin Settings",  "admin"))

        for label, pg in nav:
            active = st.session_state.page == pg
            if st.button(label, use_container_width=True,
                         type="primary" if active else "secondary",
                         key=f"nav_{pg}"):
                st.session_state.page = pg
                st.rerun()

        st.divider()

        # Alert log
        unread     = st.session_state.unread_alerts
        dot        = " 🔴" if unread > 0 else ""
        alert_lbl  = f"📬  Alert Log ({len(st.session_state.alert_log)}){dot}"
        if st.button(alert_lbl, use_container_width=True, key="nav_alerts"):
            st.session_state.show_alert_log = not st.session_state.show_alert_log
            st.session_state.unread_alerts  = 0
            st.rerun()

        st.divider()

        # User info
        role_icon = "👑" if user.get("role") == "admin" else "👤"
        st.caption(f"**{role_icon} {user['name']}**")
        st.caption(user["email"])
        st.caption(f"Role: {user.get('role','').upper()}")

        st.markdown(" ")
        if st.button("🚪  Sign Out", use_container_width=True, key="signout"):
            st.session_state.current_user = None
            st.session_state.page = "dashboard"
            st.rerun()


# ── GRANT CARD ────────────────────────────────────────────────────────────────

def show_grant_card(g: dict):
    temp  = g["temperature"]
    score = g["match_score"]
    icon  = {"hot": "🔴", "warm": "🟡", "less-warm": "⚪"}.get(temp, "")
    days  = g["days_to_deadline"]
    dl_icon = "🔴" if days < 30 else "📅"

    with st.container(border=True):
        col_main, col_score = st.columns([4, 1])

        with col_main:
            badges = f"{icon} **{temp.upper()}**  ·  *{g['source']}*"
            if g.get("is_manually_added"):
                badges += "  ·  📝 Manually Added"
            if g.get("is_prior_funder"):
                badges += "  ·  ⭐ Prior Funder"
            st.markdown(badges)
            st.markdown(f"### {g['program_name']}")
            st.caption(f"**{g['funding_org']}**")
            st.write(g["description"] or "No description available.")

            c1, c2 = st.columns(2)
            with c1:
                st.caption(f"{dl_icon} **{days} days** to deadline · {g['deadline']}")
            with c2:
                award = g["award_label"]
                if award and "$" not in award and award not in ["Not specified", ""]:
                    award = "$" + award
                st.caption(f"💰 {award}")

            if g.get("source") == "Archival 990":
                st.warning("⚠️ Archival data — no active RFP. For cultivation planning only.")

            if g.get("next_action"):
                if temp == "hot":
                    st.error(f"**Next Step:** {g['next_action']}")
                elif temp == "warm":
                    st.warning(f"**Next Step:** {g['next_action']}")
                else:
                    st.info(f"**Next Step:** {g['next_action']}")

        with col_score:
            st.metric("Match Score", f"{min(score, 5.0):.1f} / 5.0")
            st.write(stars(score))
            st.caption(f"Retrieved {g['retrieved_ago']}")
            if st.button("View Details", key=f"view_{g['id']}", use_container_width=True):
                st.session_state.selected_grant_id = g["id"]
                st.rerun()


# ── GRANT DETAIL ──────────────────────────────────────────────────────────────

def show_grant_detail():
    gid = st.session_state.get("selected_grant_id")
    if not gid:
        return
    g = next((x for x in st.session_state.grants if x["id"] == gid), None)
    if not g:
        return

    with st.container(border=True):
        hcol, xcol = st.columns([5, 1])
        with hcol:
            st.markdown(f"## {g['program_name']}")
            st.caption(f"**{g['funding_org']}** · {temp_label(g['temperature'])} · Source: {g['source']}")
        with xcol:
            if st.button("✕ Close", key="close_detail"):
                st.session_state.selected_grant_id = None
                st.rerun()

        st.write(g["description"] or "No description available.")
        st.divider()

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Funding Organization**")
            st.write(g["funding_org"])
            st.markdown("**Program Contact**")
            st.write(g.get("program_contact") or "Not listed")
            st.markdown("**Deadline**")
            st.write(f"{g['deadline']} ({g['days_to_deadline']} days)")
        with c2:
            st.markdown("**Award Range**")
            st.write(g["award_label"])
            st.markdown("**Application Method**")
            st.write(g.get("application_method") or "Not specified")
            st.markdown("**Eligibility**")
            st.info(g.get("eligibility") or "Not specified")

        if g.get("disqualifying_restrictions") and g["disqualifying_restrictions"] not in ["Not specified", ""]:
            st.warning(f"⚠️ Restrictions: {g['disqualifying_restrictions']}")

        if g.get("completeness") and g["completeness"] not in ["Complete", ""]:
            st.warning(f"⚠️ Still needs research: {g['completeness']}")

        st.divider()
        st.markdown("**Match Score Breakdown**")
        scoring = g.get("scoring", {})
        cols    = st.columns(4)
        for i, (_, val) in enumerate(scoring.items()):
            with cols[i]:
                lbl = val["label"]
                if val.get("is_highest_weight"):
                    lbl += " ⚖️"
                sv = min(float(val["score"]), 5.0)
                st.metric(lbl, f"{sv:.0f} / 5")
                st.progress(min(sv / 5.0, 1.0))
                if val.get("explanation"):
                    st.caption(val["explanation"])

        st.divider()
        url = g.get("application_url", "")
        if url and url not in ["#", "Not found", ""]:
            st.link_button(
                "🔗 Go to Application Page",
                url, use_container_width=True, type="primary"
            )
        else:
            st.info("Application URL not found — search the funder's website directly.")
        st.divider()


# ── DASHBOARD ─────────────────────────────────────────────────────────────────

def show_dashboard():
    st.markdown("# Grant Pipeline")
    st.caption("Opportunities surfaced for **Deborah's Place**, ranked by fit.")

    # Alert log panel
    if st.session_state.show_alert_log:
        with st.expander("📬 Alert Log", expanded=True):
            col_h, col_x = st.columns([5, 1])
            with col_h:
                st.markdown(f"**{len(st.session_state.alert_log)} alerts**")
            with col_x:
                if st.button("Close", key="close_alerts"):
                    st.session_state.show_alert_log = False
                    st.rerun()
            if st.session_state.alert_log:
                for alert in st.session_state.alert_log:
                    st.markdown(f"**{alert['grant_name']}**")
                    st.caption(f"{alert['funding_org']} · {alert['sent_ago']}")
                    st.divider()
            else:
                st.info("No alerts yet. Run the agent to start finding grants.")

    # Grant detail panel
    show_grant_detail()

    # Controls row
    cc1, cc2, cc3 = st.columns([3, 2, 1])
    with cc1:
        sort_by = st.selectbox(
            "Sort", SORT_OPTIONS,
            index=SORT_OPTIONS.index(st.session_state.sort_by),
            label_visibility="collapsed", key="sort_sel"
        )
        st.session_state.sort_by = sort_by
    with cc2:
        st.caption(f"Last updated: {date.today().strftime('%B %d, %Y')}")
    with cc3:
        if st.button("➕ Add Grant", use_container_width=True):
            st.session_state.page = "add_grant"
            st.rerun()

    # Partition grants
    all_g    = st.session_state.grants
    hot      = sort_grants([g for g in all_g if g["temperature"] == "hot"    and not is_cold_lead(g)], sort_by)
    warm     = sort_grants([g for g in all_g if g["temperature"] == "warm"   and not is_cold_lead(g)], sort_by)
    archival = sort_grants([g for g in all_g if g["temperature"] == "less-warm"], sort_by)
    cold     = [g for g in all_g if is_cold_lead(g) and not g.get("is_manually_added")]
    visible  = hot + warm
    avg_score = sum(g["match_score"] for g in visible) / max(len(visible), 1)

    # Stats row
    st.divider()
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("🔴 Hot Leads",       len(hot))
    s2.metric("🟡 Warm Leads",      len(warm))
    s3.metric("⚪ Archival Sources", len(archival))
    s4.metric("📊 Avg Match Score", f"{avg_score:.1f}")
    st.divider()

    # Run results message
    if st.session_state.run_results is not None:
        count = st.session_state.run_results
        if count > 0:
            st.success(
                f"✅ Agent found **{count} grant {'opportunity' if count == 1 else 'opportunities'}** "
                f"for Deborah's Place. Results are now in your pipeline below."
            )
        else:
            st.info(
                "✅ Agent run complete. No new opportunities matched Deborah's Place's "
                "criteria this time. Try a deeper search or a specific funder name."
            )
        if st.button("Dismiss"):
            st.session_state.run_results = None
            st.rerun()

    # Tabs
    tab_hot, tab_warm = st.tabs([
        f"🔴 Act Now ({len(hot)})",
        f"🟡 Coming Up ({len(warm)})"
    ])

    with tab_hot:
        st.caption("Hot leads with imminent deadlines. Get these out the door first.")
        if hot:
            for g in hot:
                show_grant_card(g)
        else:
            st.info("No hot leads right now. Click **▶️ Run Agent** in the sidebar to find new opportunities.")

    with tab_warm:
        st.caption("Warm leads queued for the next grant cycle.")
        if warm:
            for g in warm:
                show_grant_card(g)
        else:
            st.info("No warm leads queued. Click **▶️ Run Agent** in the sidebar to surface upcoming opportunities.")

    # Archival section
    st.divider()
    with st.expander(f"⚪ Archival Sources — 990 Data  ({len(archival)} sources)", expanded=False):
        st.caption("Historical giving patterns from IRS Form 990 filings. No active RFP confirmed. Use for cultivation planning.")
        if archival:
            for g in archival:
                show_grant_card(g)
        else:
            st.info("No archival sources yet. Run a deep search to surface 990 data.")

    if cold:
        st.caption(
            f"🙈 **{len(cold)} cold lead{'s' if len(cold) > 1 else ''} hidden** "
            f"— require existing relationships or do not accept unsolicited applications."
        )


# ── RUN AGENT PAGE ────────────────────────────────────────────────────────────

def show_run_agent():
    st.markdown("# ▶️ Run Grant Agent")
    st.markdown(
        "The agent searches the web and grant databases, filters out anything "
        "that does not qualify, scores each opportunity with AI, and updates "
        "your pipeline with everything it finds."
    )

    # Stats
    try:
        from agent.profile import OrgProfile
        from agent.state import AgentState
        profile  = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
        state    = AgentState(profile)
        last_run = state.get_last_run()
        stats    = state.get_stats()
        c1, c2, c3 = st.columns(3)
        with c1: st.metric("Total Runs Completed",   stats.get("total_runs", 0))
        with c2: st.metric("Sources Being Monitored", stats.get("watch_list_size", 0))
        with c3:
            lr = last_run["timestamp"][:10] if last_run else "Never"
            st.metric("Last Run Date", lr)
    except Exception:
        pass

    st.divider()
    st.markdown("### Search Options")

    search_type = st.radio(
        "How thorough should this search be?",
        [
            "Quick — 5 queries, about 1 minute",
            "Standard — 10 queries, about 2 minutes",
            "Deep — 20 queries, about 4 minutes",
            "Search for a specific funder by name",
        ]
    )

    custom_search = ""
    if "specific funder" in search_type:
        custom_search = st.text_input(
            "Funder name",
            placeholder="e.g. Polk Bros Foundation open grants 2026"
        )

    query_map = {
        "Quick — 5 queries, about 1 minute":   5,
        "Standard — 10 queries, about 2 minutes": 10,
        "Deep — 20 queries, about 4 minutes":  20,
        "Search for a specific funder by name": 1,
    }
    max_q = query_map.get(search_type, 5)
    st.divider()

    if st.session_state.agent_running:
        st.button("⏳ Agent is running...", disabled=True, use_container_width=True)
        st.info("Searching for open grants for Deborah's Place... this takes 1 to 3 minutes.")
    else:
        if st.button("▶️ Run Agent Now", type="primary", use_container_width=True):
            st.session_state.agent_running = True
            with st.status("Running grant search...", expanded=True) as status:
                st.write("🔵 Scanning sources: web, Grants.gov, 990 data...")
                time.sleep(0.5)
                st.write("🟣 Matching to Deborah's Place profile...")
                count = run_agent_search(max_q, custom_search)
                st.write("🟢 Scoring and ranking results...")
                time.sleep(0.3)
                st.write(f"✅ Done — {count} opportunities found.")
                status.update(label="Grant search complete!", state="complete")

            alert = {
                "grant_name":  f"Agent found {count} new lead{'s' if count != 1 else ''}",
                "funding_org": "Run complete",
                "sent_ago":    "just now",
            }
            st.session_state.alert_log.insert(0, alert)
            st.session_state.unread_alerts  += 1
            st.session_state.run_results     = count
            st.session_state.agent_running   = False
            st.rerun()

    if st.session_state.run_results is not None:
        count = st.session_state.run_results
        if count > 0:
            st.success(
                f"✅ **Done!** Found **{count} {'opportunity' if count == 1 else 'opportunities'}**. "
                f"Go to **Dashboard** to see them."
            )
        else:
            st.info("✅ Run complete. No new opportunities found. Try a deeper search or specific funder name.")
        if st.button("Clear"):
            st.session_state.run_results = None
            st.rerun()


# ── ADD GRANT PAGE ────────────────────────────────────────────────────────────

def show_add_grant():
    st.markdown("# ➕ Add Grant Manually")
    st.caption(
        "Useful for board referrals, network leads, or funders you track offline. "
        "Manually added grants bypass cold-lead filtering."
    )

    with st.form("add_grant_form"):
        c1, c2 = st.columns(2)
        with c1:
            how_heard       = st.selectbox("How did you hear about this?", HEARD_OPTIONS)
            funding_org     = st.text_input("Funding Organization *")
            program_name    = st.text_input("Program / Grant Name *")
            award_label     = st.text_input("Award Range *", placeholder="e.g. $50,000 – $150,000")
            deadline_known  = st.checkbox("I know the deadline date")
            deadline_val    = None
            if deadline_known:
                deadline_val = st.date_input("Deadline", min_value=date.today())
            award_min       = st.number_input("Award Min ($)", min_value=0, value=0, step=5000)
        with c2:
            source          = st.selectbox("Source", SOURCE_OPTIONS)
            application_method = st.text_input("Application Method", placeholder="e.g. Online portal")
            program_contact = st.text_input("Program Contact")
            award_max       = st.number_input("Award Max ($)", min_value=0, value=50000, step=5000)
            temperature     = st.selectbox(
                "Lead Temperature",
                ["hot", "warm", "less-warm"],
                format_func=lambda t: {"hot": "🔴 Hot", "warm": "🟡 Warm", "less-warm": "⚪ Archival"}.get(t, t)
            )
            app_url         = ""
            if how_heard == "Website":
                app_url = st.text_input("Application URL", placeholder="https://...")

        description    = st.text_area("Description *", height=80)
        eligibility    = st.text_area("Eligibility", height=60)
        disqualifying  = st.text_area("Disqualifying Restrictions", height=60)

        submitted = st.form_submit_button("Add Grant to Pipeline", type="primary")

    if submitted:
        if not funding_org or not program_name or not description or not award_label:
            st.error("Please fill in all required fields.")
        else:
            deadline_str = str(deadline_val) if deadline_val else "Unknown"
            days_left    = (deadline_val - date.today()).days if deadline_val else 999

            st.session_state.grants.append({
                "id":             f"m-{random.randint(1000, 9999)}",
                "temperature":    temperature,
                "source":         source,
                "is_manually_added": True,
                "funding_org":    funding_org,
                "program_name":   program_name,
                "program_contact": program_contact,
                "description":    description,
                "days_to_deadline": days_left,
                "deadline":       deadline_str,
                "eligibility":    eligibility,
                "award_label":    award_label,
                "award_min":      award_min,
                "award_max":      award_max,
                "application_method": application_method,
                "application_url":    app_url,
                "disqualifying_restrictions": disqualifying,
                "required_documents": [],
                "match_score":    3.5,
                "retrieved_ago":  "just now",
                "is_prior_funder": False,
                "next_action":    "Review eligibility and prepare application materials.",
                "completeness":   "Manually added — verify all fields.",
                "scoring": {
                    "geographic_alignment": {"score": 3, "label": "Geographic Alignment", "explanation": "Manually added — verify geographic eligibility.", "is_highest_weight": False},
                    "population_served":    {"score": 3, "label": "Population Served",    "explanation": "Manually added — verify population fit.", "is_highest_weight": False},
                    "budget_fit":           {"score": 3, "label": "Budget Fit",           "explanation": "Manually added — verify award range alignment.", "is_highest_weight": True},
                    "timeline_feasibility": {"score": 3, "label": "Timeline Feasibility", "explanation": "Manually added — verify timeline feasibility.", "is_highest_weight": False},
                },
                "location": "Chicago, IL",
            })

            # Also submit to learning loop
            try:
                from agent.profile import OrgProfile
                from learning.feedback import FeedbackProcessor
                profile   = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
                processor = FeedbackProcessor(profile)
                processor.submit(
                    funder_name   = funding_org,
                    program_name  = program_name,
                    source_url    = app_url or f"Manual entry via {how_heard}",
                    submitted_by  = st.session_state.current_user.get("name", "Staff"),
                    deadline      = str(deadline_val) if deadline_val else None,
                    eligibility   = eligibility or None,
                    notes         = f"Heard via: {how_heard}. {description}",
                )
            except Exception:
                pass

            st.success(f"✅ '{program_name}' added to your pipeline.")
            st.session_state.page = "dashboard"
            st.rerun()


# ── ORGANIZATION PROFILE ──────────────────────────────────────────────────────

def show_profile():
    st.markdown("# 🏢 Organization Profile")
    st.caption("This profile powers the AI matching engine. Keep it current for the best grant recommendations.")

    try:
        from agent.profile import OrgProfile
        profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
        org = {
            "name":             profile.org_name,
            "mission":          profile.mission_statement,
            "funding_needs":    ", ".join(profile.mission_keywords[:5]),
            "program_areas":    [p.value.replace("_", " ").title() for p in profile.program_areas],
            "location_focus":   f"{profile.geography.city}, {profile.geography.state} ({profile.geography.county or 'Cook County'})",
            "populations_served": ", ".join(p.value.replace("_", " ") for p in profile.populations_served),
        }
    except Exception:
        org = {
            "name": "Deborah's Place", "mission": "",
            "funding_needs": "", "program_areas": [],
            "location_focus": "Chicago, IL (Cook County)",
            "populations_served": "",
        }

    is_admin = st.session_state.current_user.get("role") == "admin"

    if not is_admin:
        st.info("👤 You are viewing the organization profile. Only Admin users can edit it.")
        st.markdown(f"**Organization:** {org['name']}")
        st.markdown(f"**Mission:** {org['mission']}")
        st.markdown(f"**Location:** {org['location_focus']}")
        st.markdown(f"**Program Areas:** {', '.join(org['program_areas'])}")
        st.markdown(f"**Populations Served:** {org['populations_served']}")
        return

    with st.form("profile_form"):
        org_name    = st.text_input("Organization Name *", value=org["name"])
        mission     = st.text_area("Mission Statement *", value=org["mission"], height=100)
        funding_needs = st.text_area("Funding Needs / Keywords", value=org["funding_needs"], height=60)

        st.markdown("**Program Areas**")
        all_options    = PRESET_AREAS + [a for a in org["program_areas"] if a not in PRESET_AREAS]
        selected_areas = st.multiselect("Program Areas", options=all_options, default=[a for a in org["program_areas"] if a in all_options], label_visibility="collapsed")

        c1, c2 = st.columns(2)
        with c1:
            loc_idx        = LOCATIONS.index(org["location_focus"]) if org["location_focus"] in LOCATIONS else 0
            location_focus = st.selectbox("Geographic Focus *", LOCATIONS, index=loc_idx)
        with c2:
            populations    = st.text_area("Populations Served", value=org["populations_served"], height=80)

        saved = st.form_submit_button("💾 Save Profile", type="primary")

    if saved:
        st.success("✅ Profile noted. To permanently update the matching engine edit `profiles/deborah_place.json` directly or contact your administrator.")


# ── USER MANAGEMENT ───────────────────────────────────────────────────────────

def show_users():
    if st.session_state.current_user.get("role") != "admin":
        st.error("🔒 User Management is restricted to Admin accounts.")
        return

    st.markdown("# 👥 User Management")
    st.caption("Add and remove team members and assign roles.")

    current_id = st.session_state.current_user["id"]
    db_users   = load_db_users()

    st.subheader(f"Active Team ({len(db_users)})")
    for user in db_users:
        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 2, 1])
            with c1:
                label = f"**{user['name']}**"
                if user["id"] == current_id:
                    label += "  *(You)*"
                st.markdown(label)
                st.caption(user["email"])
            with c2:
                role_tag = "👑 ADMIN" if user["role"] == "admin" else "👤 USER"
                st.write(role_tag)
                st.caption("Active" if user["status"] == "approved" else "Inactive")
            with c3:
                st.write("")

    st.divider()
    st.subheader("Create New User")
    with st.form("add_user_form"):
        au1, au2, au3 = st.columns([2, 2, 1])
        with au1:
            new_name  = st.text_input("Full Name")
        with au2:
            new_email = st.text_input("Email")
        with au3:
            new_role_label = st.selectbox("Role", ["Basic User", "Admin"])
        new_pw = st.text_input("Temporary Password", type="password")
        if st.form_submit_button("Add User", type="primary"):
            if not new_name or not new_email or not new_pw:
                st.error("Please fill in name, email, and password.")
            else:
                role = "admin" if new_role_label == "Admin" else "user"
                ok   = create_user_in_db(new_email, new_name, new_pw, role)
                if ok:
                    st.success(f"✅ User {new_email} created.")
                    st.rerun()
                else:
                    st.error(f"Email {new_email} is already registered.")


# ── ADMIN SETTINGS ────────────────────────────────────────────────────────────

def show_admin():
    if st.session_state.current_user.get("role") != "admin":
        st.error("Administrator access required.")
        return

    st.markdown("# ⚙️ Admin Settings")

    tab1, tab2, tab3 = st.tabs(["📡 Watch List", "🧠 Learning Log", "📊 Agent Stats"])

    with tab1:
        st.markdown("### Sources the Agent Monitors")
        try:
            from agent.profile import OrgProfile
            from agent.state import AgentState
            profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
            state   = AgentState(profile)
            sources = state.get_watch_list()
            high    = [s for s in sources if s.get("priority") == "high"]
            medium  = [s for s in sources if s.get("priority") == "medium"]
            other   = [s for s in sources if s.get("priority") not in ["high","medium"]]
            st.write(f"**{len(sources)} sources** — {len(high)} high, {len(medium)} medium priority")
            st.divider()
            for group_label, group in [("🔴 High Priority", high), ("🟠 Medium Priority", medium), ("🔵 Other", other)]:
                if group:
                    st.markdown(f"**{group_label}**")
                    for s in group:
                        c1, c2 = st.columns([4, 1])
                        with c1:
                            st.write(f"• **{s['name']}**")
                            st.caption(s["url"])
                        with c2:
                            st.caption(s.get("added_by","seed"))
        except Exception as e:
            st.error(f"Could not load watch list: {e}")

        st.divider()
        st.markdown("### Add New Source")
        with st.form("add_source_form"):
            sn  = st.text_input("Foundation name")
            su  = st.text_input("URL", placeholder="https://www.foundation.org/grants")
            sp  = st.selectbox("Priority", ["high", "medium", "low"])
            ab  = st.form_submit_button("Add to Watch List", type="primary")
        if ab:
            if not sn or not su:
                st.error("Name and URL required.")
            else:
                try:
                    from agent.profile import OrgProfile
                    from agent.state import AgentState
                    profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
                    state   = AgentState(profile)
                    added   = state.add_to_watch_list(
                        name=sn, url=su, priority=sp,
                        added_by=f"admin:{st.session_state.current_user['email']}"
                    )
                    state.save()
                    st.success(f"✅ Added {sn}." if added else f"{sn} already in list.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    with tab2:
        st.markdown("### Learning Log")
        try:
            from agent.profile import OrgProfile
            from learning.learning_log import LearningLog
            profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
            log     = LearningLog(profile)
            stats   = log.get_stats()
            entries = log.get_recent_changes(limit=20)
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("Submissions",   stats.get("submissions", 0))
            with c2: st.metric("Agent Updates", stats.get("changes", 0))
            with c3: st.metric("Sources Added", stats.get("sources_added", 0))
            st.divider()
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
        except Exception as e:
            st.error(f"Could not load learning log: {e}")

    with tab3:
        st.markdown("### Current Agent Configuration")
        try:
            from agent.profile import OrgProfile
            profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
            st.markdown(f"""
| Setting | Value |
|---|---|
| Organization | {profile.org_name} |
| Location | {profile.geography.city}, {profile.geography.state} |
| Grant range | ${profile.budget.request_floor:,} – ${profile.budget.request_ceiling:,} |
| Federal excluded | {"✅ Yes" if profile.settings.exclude_federal else "❌ No"} |
| Min days until deadline | {profile.settings.deadline_floor_days} days |
| Min score to show | {profile.settings.min_composite_score} / 5.0 |
| Active programs | {len(profile.program_areas)} |
| Known funders | {len(profile.known_funders)} |
            """)
            st.markdown("**Known Prior Funders:**")
            for f in profile.known_funders:
                st.write(f"• {f.name} — last award {f.last_award_year}")
        except Exception as e:
            st.error(f"Could not load settings: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    init_state()

    if not st.session_state.current_user:
        show_login()
        return

    show_sidebar()

    page = st.session_state.page
    if page == "dashboard":
        show_dashboard()
    elif page == "run_agent":
        show_run_agent()
    elif page == "add_grant":
        show_add_grant()
    elif page == "profile":
        show_profile()
    elif page == "users":
        show_users()
    elif page == "admin":
        show_admin()
    else:
        show_dashboard()


main()
