import streamlit as st
from datetime import datetime, timedelta
import time
import random

st.set_page_config(
    page_title="GrantScout AI",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def dl(days: int) -> str:
    d = datetime.now() + timedelta(days=days)
    return f"{d.strftime('%b')} {d.day}, {d.year}"

def is_cold_lead(g: dict) -> bool:
    if g.get("is_manually_added"):
        return False
    if g["temperature"] == "less-warm":
        return False
    r = g["disqualifying_restrictions"].lower()
    m = g["application_method"].lower()
    return (
        "unsolicited applications declined" in r
        or "does not accept unsolicited" in r
        or "by invitation only" in m
    )

def sort_grants(grants: list, sort_by: str) -> list:
    key_map = {
        "Match Score: High to Low": (lambda g: g["match_score"], True),
        "Match Score: Low to High": (lambda g: g["match_score"], False),
        "Deadline: Soonest First": (lambda g: g["days_to_deadline"], False),
        "Award Amount: Highest First": (lambda g: g["award_max"], True),
        "Award Amount: Lowest First": (lambda g: g["award_max"], False),
    }
    fn, rev = key_map.get(sort_by, (lambda g: g["match_score"], True))
    return sorted(grants, key=fn, reverse=rev)

def stars(score: float) -> str:
    full = int(round(score))
    return "★" * full + "☆" * (5 - full)

def temp_label(temp: str) -> str:
    return {"hot": "🔴 Hot", "warm": "🟡 Warm", "less-warm": "⚪ Archival"}.get(temp, temp)

# Above this many sources, show a dropdown selector instead of a row of buttons.
MULTI_SOURCE_DROPDOWN_THRESHOLD = 3

def get_sources(g: dict) -> list:
    """
    Normalize a grant's source(s) into a list of
    {"name", "url", "required_documents"} dicts.
    Falls back to the single legacy source/source_url/required_documents
    fields when a grant doesn't define an explicit "sources" list.
    """
    if g.get("sources"):
        return g["sources"]
    return [{
        "name": g.get("source", "Unknown"),
        "url": g.get("source_url", ""),
        "required_documents": g.get("required_documents", []),
    }]

def analyze_required_documents(sources: list):
    """
    Union required documents across all sources for a grant, and flag any
    document that isn't mentioned by every source as a conflict — surfaced
    separately so staff know to verify it directly with the funder.

    Returns (all_docs: sorted list, conflicts: {doc: {"mentioned_by": [...], "not_mentioned_by": [...]}})
    """
    by_source = {s["name"]: set(s.get("required_documents") or []) for s in sources}
    all_docs = sorted(set.union(*by_source.values())) if by_source else []
    conflicts = {}
    for doc in all_docs:
        mentioned_by = [name for name, docs in by_source.items() if doc in docs]
        not_mentioned_by = [name for name in by_source if name not in mentioned_by]
        if not_mentioned_by:
            conflicts[doc] = {"mentioned_by": mentioned_by, "not_mentioned_by": not_mentioned_by}
    return all_docs, conflicts

def estimate_box_height(text: str, chars_per_line: int = 90, line_px: int = 27, padding_px: int = 30, min_px: int = 320, max_px: int = 700) -> int:
    """Rough pixel height so a text_area fits its content without an internal scrollbar."""
    lines = 0
    for line in text.split("\n"):
        lines += max(1, -(-len(line) // chars_per_line))  # ceil division; blank lines still count as 1
    return max(min_px, min(max_px, lines * line_px + padding_px))

def _contact_greeting(program_contact: str) -> str:
    """Best-effort salutation name from a 'Name — Title' contact string."""
    lowered = program_contact.lower()
    if "—" in program_contact and not any(
        phrase in lowered for phrase in ["not published", "pending", "public 990 record"]
    ):
        return program_contact.split("—")[0].strip()
    return "Grants Team"

def generate_email_draft(g: dict, org: dict, extra_info: str = "") -> str:
    """Draft a copy-paste-ready inquiry email: org intro, interest + fit case,
    a direct fit question, then a close asking about required documents
    (surfacing any cross-source conflicts so the funder can clarify)."""
    sources = get_sources(g)
    _, conflicts = analyze_required_documents(sources)
    source_names = " and ".join(s["name"] for s in sources)
    contact_name = _contact_greeting(g.get("program_contact", ""))

    fit_reasons = [
        val["explanation"] for val in g.get("scoring", {}).values() if val.get("score", 0) >= 4
    ]
    fit_paragraph = " ".join(fit_reasons[:2]) or "Our mission and program focus closely align with your funding priorities."

    intro = f"My name is [Your Name], [Your Title] at {org['name']}. {org['mission']}"
    if org.get("funding_needs"):
        intro += f" We are currently seeking support for {org['funding_needs'][0].lower()}{org['funding_needs'][1:]}"
    if extra_info.strip():
        intro += f" {extra_info.strip()}"

    focus = ", ".join(org.get("program_areas", [])) or org.get("populations_served", "our mission")

    closing_ask = "If we are a good fit, we were curious about the required documents and information needed to submit an application."
    if conflicts:
        conflict_list = "; ".join(conflicts.keys())
        closing_ask += (
            f" We noticed the listed requirements differ slightly across sources (specifically: {conflict_list}) "
            "and would appreciate your guidance on the complete, current checklist."
        )

    return f"""Subject: Inquiry Regarding {g['program_name']} — {org['name']}

Dear {contact_name},

{intro}

We recently learned about your {g['program_name']} opportunity through {source_names} and are very interested in applying. {fit_paragraph}

Given our focus on {focus}, we believe {org['name']} would be a strong fit for this grant. We would welcome the chance to share more about our program and to hear your thoughts on whether we might be a good fit for this opportunity.

{closing_ask}

Thank you very much for your time and consideration. We look forward to hearing from you.

Sincerely,
[Your Name]
[Your Title]
{org['name']}
[Your Email]
"""

# ── Initial data ──────────────────────────────────────────────────────────────

INITIAL_GRANTS = [
    {
        "id": "1", "temperature": "hot", "source": "Philanthropy News Digest",
        "source_url": "https://philanthropynewsdigest.org/",
        "funding_org": "The Chicago Community Trust", "program_name": "Community Impact Grant",
        "program_contact": "Maria Velasquez — Senior Program Officer, Basic Human Needs",
        "description": "Unrestricted operating support for Chicago nonprofits delivering direct services to populations facing housing instability and economic hardship.",
        "days_to_deadline": 11, "deadline": dl(11),
        "eligibility": "501(c)(3) headquartered in Cook County. Annual budget $1M–$15M. Demonstrated direct-service track record of 3+ years.",
        "award_label": "$50,000 – $150,000 (one year, renewable)", "award_min": 50000, "award_max": 150000,
        "application_method": "Online portal via CCT Grantee Hub", "application_url": "#",
        "disqualifying_restrictions": "Does not fund capital campaigns, endowments, or sectarian religious activities.",
        "required_documents": ["Organizational budget (current FY)", "Most recent audited financials", "Board roster", "Program logic model", "IRS determination letter"],
        "sources": [
            {
                "name": "Philanthropy News Digest", "url": "https://philanthropynewsdigest.org/",
                "required_documents": ["Organizational budget (current FY)", "Most recent audited financials", "Board roster", "Program logic model", "IRS determination letter"],
            },
            {
                "name": "GrantStation", "url": "https://grantstation.com/",
                "required_documents": ["Organizational budget (current FY)", "Board roster", "IRS determination letter", "Letter of support from a partner agency"],
            },
        ],
        "match_score": 4.8, "retrieved_ago": "23 hours ago", "is_manually_added": False,
        "scoring": {
            "geographic_alignment": {"score": 5, "label": "Geographic Alignment", "explanation": "Chicago/Cook County is the funder's exclusive service area — perfect match for Deborah's Place."},
            "population_served": {"score": 5, "label": "Population Served", "explanation": "Explicitly funds women experiencing homelessness and trauma-informed housing services."},
            "budget_fit": {"score": 5, "label": "Budget Fit", "explanation": "Award ceiling of $150K aligns with mid-size operating budget cycles of 3+ years.", "is_highest_weight": True},
            "timeline_feasibility": {"score": 4, "label": "Timeline Feasibility", "explanation": "Deadline in 11 days — tight but feasible. LOI not required."},
        },
        "location": "Cook County, IL",
    },
    {
        "id": "2", "temperature": "hot", "source": "Manual Entry", "is_manually_added": True,
        "source_url": "",
        "funding_org": "Conrad N. Hilton Foundation", "program_name": "Catholic Sisters Initiative — Local Partner",
        "program_contact": "Board member referral — contact info pending",
        "description": "Lead from board member — funder considering Chicago-area expansion for women's housing services. Requires concept paper by end of quarter.",
        "days_to_deadline": 28, "deadline": dl(28),
        "eligibility": "Invitation-based. Prior relationship with sisters organization preferred.",
        "award_label": "$25,000 – $250,000", "award_min": 25000, "award_max": 250000,
        "application_method": "Direct submission via program officer contact", "application_url": "#",
        "disqualifying_restrictions": "Requires prior relationship with Catholic Sisters network.",
        "required_documents": ["Concept paper (3–5 pages)", "Organization overview"],
        "match_score": 4.4, "retrieved_ago": "22 hours ago",
        "scoring": {
            "geographic_alignment": {"score": 5, "label": "Geographic Alignment", "explanation": "Funder expanding to Chicago; geographic alignment is explicit."},
            "population_served": {"score": 4, "label": "Population Served", "explanation": "Strong fit for women's housing; Catholic affiliation not required for partners."},
            "budget_fit": {"score": 4, "label": "Budget Fit", "explanation": "Wide award range; concept paper stage keeps commitment light.", "is_highest_weight": True},
            "timeline_feasibility": {"score": 5, "label": "Timeline Feasibility", "explanation": "28-day window for concept paper is very manageable."},
        },
        "location": "Chicago, IL",
    },
    {
        "id": "3", "temperature": "warm", "source": "Instrumentl", "is_manually_added": False,
        "source_url": "https://www.instrumentl.com/",
        "funding_org": "Polk Bros. Foundation", "program_name": "Social Services Partnership",
        "program_contact": "Daniel Park — Program Director, Strong Communities",
        "description": "Multi-year general operating and program support for Chicago organizations addressing the root causes of poverty.",
        "days_to_deadline": 62, "deadline": dl(62),
        "eligibility": "Chicago-based 501(c)(3) with prior funder relationship preferred. Focus on housing, education, or workforce.",
        "award_label": "$75,000 – $200,000 over 2 years", "award_min": 75000, "award_max": 200000,
        "application_method": "Invited LOI, followed by full proposal", "application_url": "#",
        "disqualifying_restrictions": "No individual scholarships. No fiscal sponsors.",
        "required_documents": ["Letter of Inquiry (3 pages)", "Two-year program budget", "Outcomes measurement plan"],
        "match_score": 4.2, "retrieved_ago": "1 day ago",
        "scoring": {
            "geographic_alignment": {"score": 5, "label": "Geographic Alignment", "explanation": "Chicago-only funder; geographic fit is ideal."},
            "population_served": {"score": 4, "label": "Population Served", "explanation": "Housing & women's services within stated priorities."},
            "budget_fit": {"score": 4, "label": "Budget Fit", "explanation": "2-year award size matches mid-tier program needs.", "is_highest_weight": True},
            "timeline_feasibility": {"score": 4, "label": "Timeline Feasibility", "explanation": "Cycle opens in ~2 months — adequate runway to prepare strong LOI."},
        },
        "location": "Chicago, IL",
    },
    {
        "id": "4", "temperature": "less-warm", "source": "Archival 990", "is_manually_added": False,
        "source_url": "https://apps.irs.gov/app/eos/",
        "funding_org": "MacArthur Foundation", "program_name": "Historical Giving — Housing & Human Services",
        "program_contact": "Public 990 Record — Contact info not published",
        "description": "Historical pattern of giving to Chicago housing organizations identified from 2021–2023 IRS Form 990 filings. No active RFP at this time; relationship-building recommended.",
        "days_to_deadline": 180, "deadline": dl(180),
        "eligibility": "Inferred from historical grantee list. Funder does not accept unsolicited proposals.",
        "award_label": "$100,000 – $500,000 (historical median)", "award_min": 100000, "award_max": 500000,
        "application_method": "By invitation only — cultivation strategy required", "application_url": "#",
        "disqualifying_restrictions": "Unsolicited applications declined. Archival data only — verify against current funder strategy.",
        "required_documents": ["N/A — relationship-building phase"],
        "match_score": 2.9, "retrieved_ago": "2 days ago",
        "scoring": {
            "geographic_alignment": {"score": 5, "label": "Geographic Alignment", "explanation": "Historical Chicago grantees concentrated in similar service area."},
            "population_served": {"score": 3, "label": "Population Served", "explanation": "Funded adjacent housing orgs; specific women's focus is uncertain."},
            "budget_fit": {"score": 3, "label": "Budget Fit", "explanation": "Median historical award fits, but no guarantee funder remains in this issue area.", "is_highest_weight": True},
            "timeline_feasibility": {"score": 1, "label": "Timeline Feasibility", "explanation": "No open cycle. Cultivation typically requires 12–18 months."},
        },
        "location": "Chicago, IL",
    },
]

INITIAL_USERS = [
    {"id": "u1", "name": "Alex Morgan", "email": "alex@deborahsplace.org", "role": "admin", "status": "approved", "password": "demo1234", "organization": "Deborah's Place", "title": "Executive Director"},
    {"id": "u2", "name": "Priya Shah", "email": "priya@deborahsplace.org", "role": "basic", "status": "approved", "password": "demo1234", "organization": "Deborah's Place", "title": "Development Associate"},
    {"id": "u3", "name": "Jordan Lee", "email": "jordan@deborahsplace.org", "role": "basic", "status": "approved", "password": "demo1234", "organization": "Deborah's Place", "title": "Program Coordinator"},
]

INITIAL_ORG = {
    "name": "Deborah's Place",
    "mission": "Deborah's Place opens doors of opportunity for women experiencing homelessness in Chicago. We provide supportive housing and services to help women heal, grow, and move beyond homelessness.",
    "funding_needs": "General operating support, supportive housing services, mental health & trauma-informed care programs, workforce readiness.",
    "program_areas": ["Permanent Supportive Housing", "Interim Housing", "Health & Wellness", "Community Engagement"],
    "location_focus": "Chicago, IL (Cook County)",
    "budget_range": "$5M – $10M",
    "populations_served": "Adult women (18+) experiencing chronic homelessness, including survivors of trauma, women with disabilities, and women in recovery.",
}

INITIAL_ALERTS = [
    {"id": "a1", "grant_name": "Community Impact Grant", "funding_org": "The Chicago Community Trust", "sent_ago": "19 mins ago", "recipients": 3},
    {"id": "a2", "grant_name": "Community Impact Grant", "funding_org": "The Chicago Community Trust", "sent_ago": "23 hours ago", "recipients": 3},
]

PRESET_AREAS = [
    "Permanent Supportive Housing", "Interim Housing", "Health & Wellness",
    "Community Engagement", "Workforce Development", "Mental Health Services",
    "Youth Services", "Education", "Women's Services", "Anti-Poverty",
    "Legal Aid", "Substance Use Recovery",
]

BUDGET_RANGES = [
    "$0 – $25K", "$25K – $100K", "$100K – $500K", "$500K – $1M",
    "$1M – $2M", "$2M – $5M", "$5M – $10M", "$10M – $25M", "$25M – $50M", "Over $50M",
]

LOCATIONS = ["Chicago, IL (Cook County)", "Greater Chicago Metro", "Illinois Statewide", "National"]

SORT_OPTIONS = [
    "Match Score: High to Low",
    "Match Score: Low to High",
    "Deadline: Soonest First",
    "Award Amount: Highest First",
    "Award Amount: Lowest First",
]

# ── State init ────────────────────────────────────────────────────────────────

def init_state():
    defaults = {
        "current_user": None,
        "page": "dashboard",
        "grants": [g.copy() for g in INITIAL_GRANTS],
        "users": [u.copy() for u in INITIAL_USERS],
        "pending_users": [],
        "org_profile": INITIAL_ORG.copy(),
        "alert_log": [a.copy() for a in INITIAL_ALERTS],
        "unread_alerts": 0,
        "selected_grant_id": None,
        "sort_by": "Match Score: High to Low",
        "show_alert_log": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

# ── Login ─────────────────────────────────────────────────────────────────────

def show_login():
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("## 🎯 GrantScout AI")
        st.caption("AI-Powered Grant Prospecting Agent")
        st.divider()

        tab_in, tab_up = st.tabs(["Sign In", "Sign Up"])

        with tab_in:
            with st.form("signin"):
                email = st.text_input("Email", placeholder="you@organization.org")
                password = st.text_input("Password", type="password", placeholder="••••••••")
                go = st.form_submit_button("Sign In", type="primary", use_container_width=True)
            if go:
                pending = next((u for u in st.session_state.pending_users if u["email"].lower() == email.lower()), None)
                if pending:
                    st.error("Your account is pending admin approval.")
                else:
                    user = next((u for u in st.session_state.users if u["email"].lower() == email.lower()), None)
                    if not user:
                        st.error("No account found with that email address.")
                    elif user.get("password") and user["password"] != password:
                        st.error("Incorrect password. Please try again.")
                    else:
                        st.session_state.current_user = user.copy()
                        st.session_state.page = "dashboard"
                        st.rerun()
            st.caption("Demo: **alex@deborahsplace.org** · password: `demo1234`")

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
                    su_name = st.text_input("Full Name *")
                    su_email = st.text_input("Work Email *")
                    su_pw = st.text_input("Password *", type="password")
                with c2:
                    su_title = st.text_input("Job Title *")
                    su_org = st.text_input("Organization *")
                    su_pw2 = st.text_input("Confirm Password *", type="password")
                su_phone = st.text_input("Phone (optional)", placeholder="(312) 555-0000")

                if is_admin:
                    st.divider()
                    st.markdown("**Organization Profile**")
                    op_name = st.text_input("Organization Name *")
                    op_mission = st.text_area("Mission Statement *", height=80)
                    op_needs = st.text_area("Funding Needs", height=60)
                    op_loc = st.selectbox("Geographic Focus *", LOCATIONS)
                    op_budget = st.selectbox("Budget Range *", BUDGET_RANGES)
                    op_pop = st.text_area("Populations Served", height=60)

                submitted = st.form_submit_button(
                    "Create Account" if is_admin else "Submit Request",
                    type="primary",
                    use_container_width=True,
                )

            if submitted:
                all_emails = [u["email"].lower() for u in st.session_state.users + st.session_state.pending_users]
                if not su_name or not su_email or not su_pw or not su_org or not su_title:
                    st.error("Please fill in all required fields.")
                elif su_pw != su_pw2:
                    st.error("Passwords do not match.")
                elif len(su_pw) < 6:
                    st.error("Password must be at least 6 characters.")
                elif su_email.lower() in all_emails:
                    st.error("An account with that email already exists.")
                else:
                    new_user = {
                        "id": f"u{random.randint(1000, 9999)}",
                        "name": su_name, "email": su_email,
                        "role": "admin" if is_admin else "basic",
                        "status": "approved" if is_admin else "pending",
                        "password": su_pw, "organization": su_org,
                        "title": su_title, "phone": su_phone,
                    }
                    if is_admin:
                        st.session_state.users.append(new_user)
                        st.session_state.current_user = new_user.copy()
                        st.session_state.org_profile = {
                            "name": op_name or su_org, "mission": op_mission,
                            "funding_needs": op_needs, "program_areas": [],
                            "location_focus": op_loc, "budget_range": op_budget,
                            "populations_served": op_pop,
                        }
                        st.session_state.page = "dashboard"
                        st.rerun()
                    else:
                        st.session_state.pending_users.append(new_user)
                        st.success("✅ Request submitted. An admin will review and approve your access.")

# ── Sidebar ───────────────────────────────────────────────────────────────────

def show_sidebar():
    user = st.session_state.current_user
    with st.sidebar:
        st.markdown("## 🎯 GrantScout AI")
        st.caption("Prospecting Agent")
        st.success("● Agent Active · Last checked 42 min ago")
        st.divider()

        # Navigation
        pending_count = len(st.session_state.pending_users)
        nav = [
            ("🏠  Dashboard", "dashboard"),
            ("🏢  Organization Profile", "profile"),
            ("➕  Add Grant Manually", "add_grant"),
        ]
        if user.get("role") == "admin":
            badge = f" ({pending_count} pending)" if pending_count else ""
            nav.append((f"👥  User Management{badge}", "users"))

        for label, pg in nav:
            is_active = st.session_state.page == pg
            if st.button(label, use_container_width=True, type="primary" if is_active else "secondary", key=f"nav_{pg}"):
                st.session_state.page = pg
                st.rerun()

        st.divider()

        # Alert log toggle
        unread = st.session_state.unread_alerts
        dot = " 🔴" if unread > 0 else ""
        alert_label = f"📬  Alert Log ({len(st.session_state.alert_log)}){dot}"
        if st.button(alert_label, use_container_width=True, key="nav_alerts"):
            st.session_state.show_alert_log = not st.session_state.show_alert_log
            st.session_state.unread_alerts = 0
            st.rerun()

        st.divider()

        # User info
        st.caption(f"**{user['name']}**")
        st.caption(user["email"])
        if user.get("title"):
            st.caption(user["title"])

        # Demo role switcher
        st.caption("🎭 *Demo — switch role:*")
        roles = ["Admin", "Basic User"]
        cur_idx = 0 if user["role"] == "admin" else 1
        new_role_label = st.selectbox("Role", roles, index=cur_idx, label_visibility="collapsed", key="role_sw")
        new_role = "admin" if new_role_label == "Admin" else "basic"
        if new_role != user["role"]:
            st.session_state.current_user["role"] = new_role
            for u in st.session_state.users:
                if u["id"] == user["id"]:
                    u["role"] = new_role
            st.rerun()

        if st.button("🚪  Sign Out", use_container_width=True, key="signout"):
            st.session_state.current_user = None
            st.session_state.page = "dashboard"
            st.rerun()

# ── Grant card ────────────────────────────────────────────────────────────────

def show_grant_card(g: dict):
    temp = g["temperature"]
    score = g["match_score"]
    icon = {"hot": "🔴", "warm": "🟡", "less-warm": "⚪"}.get(temp, "")
    days = g["days_to_deadline"]
    dl_icon = "🔴" if days < 30 else "📅"

    with st.container(border=True):
        col_main, col_score = st.columns([4, 1])
        with col_main:
            badges = f"{icon} **{temp.upper()}**  ·  *{g['source']}*"
            if g.get("is_manually_added"):
                badges += "  ·  📝 Manually Added"
            st.markdown(badges)
            st.markdown(f"### {g['program_name']}")
            st.caption(f"**{g['funding_org']}**")
            st.write(g["description"])
            c1, c2 = st.columns(2)
            with c1:
                st.caption(f"{dl_icon} **{days} days** to deadline · {g['deadline']}")
            with c2:
                st.caption(f"💰 {g['award_label']}")
            if g.get("source") == "Archival 990":
                st.warning("⚠️ Archival data — no active RFP. For cultivation planning only.")
        with col_score:
            st.metric("Match Score", f"{score:.1f} / 5.0")
            st.write(stars(score))
            st.caption(f"Retrieved {g['retrieved_ago']}")
            if st.button("View Details", key=f"view_{g['id']}", use_container_width=True):
                st.session_state.selected_grant_id = g["id"]
                st.rerun()

# ── Grant detail ──────────────────────────────────────────────────────────────

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
            sources = get_sources(g)
            source_names = " · ".join(s["name"] for s in sources)
            st.caption(f"**{g['funding_org']}** · {temp_label(g['temperature'])} · Source: {source_names}")

            if len(sources) == 1:
                if sources[0].get("url"):
                    st.link_button(f"🔗 View on {sources[0]['name']}", sources[0]["url"])
            elif len(sources) <= MULTI_SOURCE_DROPDOWN_THRESHOLD:
                link_cols = st.columns(len(sources))
                for col, s in zip(link_cols, sources):
                    with col:
                        if s.get("url"):
                            st.link_button(f"🔗 {s['name']}", s["url"], use_container_width=True)
            else:
                chosen_name = st.selectbox(
                    "Found on multiple sources — view listing:",
                    [s["name"] for s in sources],
                    key=f"src_sel_{g['id']}",
                )
                chosen = next(s for s in sources if s["name"] == chosen_name)
                if chosen.get("url"):
                    st.link_button(f"🔗 View on {chosen_name}", chosen["url"])
        with xcol:
            if st.button("✕ Close", key="close_detail"):
                st.session_state.selected_grant_id = None
                st.rerun()

        st.write(g["description"])
        st.divider()

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Funding Organization**")
            st.write(g["funding_org"])
            st.markdown("**Program Contact**")
            st.write(g["program_contact"])
            st.markdown("**Deadline**")
            st.write(f"{g['deadline']} ({g['days_to_deadline']} days)")
        with c2:
            st.markdown("**Award Range**")
            st.write(g["award_label"])
            st.markdown("**Application Method**")
            st.write(g["application_method"])
            st.markdown("**Eligibility**")
            st.info(g["eligibility"])

        if g["disqualifying_restrictions"]:
            st.warning(f"⚠️ Restrictions: {g['disqualifying_restrictions']}")

        st.divider()
        st.markdown("**Match Score Breakdown**")
        scoring = g["scoring"]
        cols = st.columns(4)
        for i, (_, val) in enumerate(scoring.items()):
            with cols[i]:
                lbl = val["label"]
                if val.get("is_highest_weight"):
                    lbl += " ⚖️"
                st.metric(lbl, f"{val['score']} / 5")
                st.caption(val["explanation"])

        st.divider()
        st.markdown("**Required Documents**")
        all_docs, conflicts = analyze_required_documents(sources)
        for doc in all_docs:
            flag = " ⚠️" if doc in conflicts else ""
            st.write(f"• {doc}{flag}")

        if conflicts:
            st.divider()
            st.markdown("**⚠️ Conflicting Requirements Across Sources**")
            st.caption("These items are mentioned by some sources but not others — verify directly with the funder before finalizing your checklist.")
            for doc, info in conflicts.items():
                mentioned = ", ".join(info["mentioned_by"])
                missing = ", ".join(info["not_mentioned_by"])
                st.warning(f"**{doc}** — listed by {mentioned}; not mentioned by {missing}")

        st.divider()
        st.markdown("**✉️ Draft Inquiry Email**")
        caption = "Ready to copy and send. Introduces your organization, shows interest, makes the case for fit, then asks about required documents."
        if conflicts:
            caption += " Since sources disagree on requirements above, it also asks the funder to confirm the full checklist."
        st.caption(caption)

        with st.expander("✉️ Draft Inquiry Email", expanded=True):
            extra_info = st.text_area(
                "Add more about your organization to weave into the draft (optional)",
                key=f"email_extra_{g['id']}", height=80,
                placeholder="e.g. recent program outcomes, specific alignment notes...",
            )
            draft = generate_email_draft(g, st.session_state.org_profile, extra_info)
            st.markdown(
                "<style>.st-key-email_draft_box textarea { line-height: 1.5 !important; font-family: inherit !important; }</style>",
                unsafe_allow_html=True,
            )
            st.text_area(
                "Email draft", value=draft, height=estimate_box_height(draft),
                key="email_draft_box", label_visibility="collapsed",
            )
            st.caption("Click inside, select all (Ctrl+A / Cmd+A), and copy — fill in the bracketed placeholders before sending. GrantScout does not send emails automatically.")
        st.divider()

# ── Dashboard ─────────────────────────────────────────────────────────────────

def _make_agent_grants() -> list:
    return [
        {
            "id": f"ag-{random.randint(10000, 99999)}", "temperature": "hot",
            "source": "Grants.gov", "source_url": "https://www.grants.gov/", "is_manually_added": False,
            "funding_org": "Richard H. Driehaus Foundation", "program_name": "Housing Opportunity Grant",
            "program_contact": "Grants Office — grants@driehausfdn.org",
            "description": "Direct grants to Chicago organizations providing transitional and supportive housing for vulnerable adults, with emphasis on trauma-informed service models.",
            "days_to_deadline": 18, "deadline": dl(18),
            "eligibility": "501(c)(3) Chicago-based organization serving adults. Min. 2 years operating history. Annual budget $500K–$20M.",
            "award_label": "$30,000 – $100,000 (one year)", "award_min": 30000, "award_max": 100000,
            "application_method": "Online application via Driehaus Foundation portal", "application_url": "#",
            "disqualifying_restrictions": "Does not fund religious organizations for religious purposes or capital campaigns.",
            "required_documents": ["Narrative application (5 pages)", "Current year budget", "Board member list", "IRS determination letter"],
            "match_score": 4.6, "retrieved_ago": "just now",
            "scoring": {
                "geographic_alignment": {"score": 5, "label": "Geographic Alignment", "explanation": "Chicago-only funder; geographic fit is ideal."},
                "population_served": {"score": 5, "label": "Population Served", "explanation": "Explicitly funds trauma-informed housing for vulnerable adults."},
                "budget_fit": {"score": 4, "label": "Budget Fit", "explanation": "Award ceiling at $100K fits budget range; one-year award manageable.", "is_highest_weight": True},
                "timeline_feasibility": {"score": 5, "label": "Timeline Feasibility", "explanation": "18 days to deadline — achievable with existing materials."},
            },
            "location": "Chicago, IL",
        },
        {
            "id": f"ag-{random.randint(10000, 99999)+1}", "temperature": "warm",
            "source": "Philanthropy News Digest", "source_url": "https://philanthropynewsdigest.org/", "is_manually_added": False,
            "funding_org": "Woods Fund Chicago", "program_name": "Community Equity Program",
            "program_contact": "Program Staff — info@woodsfund.org",
            "description": "Multi-year support for Chicago organizations advancing racial and economic equity through direct services and advocacy, including housing stability programs.",
            "days_to_deadline": 90, "deadline": dl(90),
            "eligibility": "Chicago-based 501(c)(3). Focus areas include housing, economic mobility, and community health.",
            "award_label": "$50,000 – $150,000 over 2 years", "award_min": 50000, "award_max": 150000,
            "application_method": "Letter of Inquiry submission via online portal", "application_url": "#",
            "disqualifying_restrictions": "Does not fund direct religious programming or individual scholarships.",
            "required_documents": ["Letter of Inquiry (2 pages)", "Organizational overview", "Prior year financials"],
            "match_score": 3.9, "retrieved_ago": "just now",
            "scoring": {
                "geographic_alignment": {"score": 5, "label": "Geographic Alignment", "explanation": "Chicago-only funder aligned with Deborah's Place service area."},
                "population_served": {"score": 4, "label": "Population Served", "explanation": "Housing and economic equity focus aligns well with mission."},
                "budget_fit": {"score": 3, "label": "Budget Fit", "explanation": "Two-year award is in range; LOI stage keeps near-term investment low.", "is_highest_weight": True},
                "timeline_feasibility": {"score": 4, "label": "Timeline Feasibility", "explanation": "90-day window gives adequate runway to prepare a strong LOI."},
            },
            "location": "Chicago, IL",
        },
        {
            "id": f"ag-{random.randint(10000, 99999)+2}", "temperature": "warm",
            "source": "GrantStation", "source_url": "https://grantstation.com/", "is_manually_added": False,
            "funding_org": "Steans Family Foundation", "program_name": "Neighborhood Stability Fund",
            "program_contact": "Grants Committee — info@steansfamilyfoundation.org",
            "description": "Support for Chicago nonprofits stabilizing housing and building economic resilience in North Lawndale and surrounding communities.",
            "days_to_deadline": 45, "deadline": dl(45),
            "eligibility": "Chicago-based 501(c)(3) working in North Lawndale or adjacent communities. Focus on housing, workforce, or education.",
            "award_label": "$20,000 – $75,000 (one year)", "award_min": 20000, "award_max": 75000,
            "application_method": "Online application via GrantStation-listed portal", "application_url": "#",
            "disqualifying_restrictions": "Does not fund organizations outside of designated Chicago neighborhoods.",
            "required_documents": ["Application narrative (4 pages)", "Current year budget", "IRS determination letter"],
            "match_score": 3.8, "retrieved_ago": "just now",
            "scoring": {
                "geographic_alignment": {"score": 4, "label": "Geographic Alignment", "explanation": "Neighborhood-specific focus; verify Deborah's Place program sites fall within target area."},
                "population_served": {"score": 4, "label": "Population Served", "explanation": "Housing stability focus aligns with supportive housing mission."},
                "budget_fit": {"score": 4, "label": "Budget Fit", "explanation": "One-year award size fits smaller program-specific budgets.", "is_highest_weight": True},
                "timeline_feasibility": {"score": 4, "label": "Timeline Feasibility", "explanation": "45-day window is comfortable for a standard application."},
            },
            "location": "Chicago, IL",
        },
    ]

def _make_archival_grant() -> dict:
    return {
        "id": f"arc-{random.randint(10000, 99999)}", "temperature": "less-warm",
        "source": "Archival 990", "source_url": "https://apps.irs.gov/app/eos/", "is_manually_added": False,
        "funding_org": "Wintrust Community Advantage", "program_name": "Affordable Housing Fund — Historical Data",
        "program_contact": "Public 990 Record — Contact info not published",
        "description": "Historical CRA-qualified grant-making to Chicago affordable housing providers identified from 2020–2023 IRS Form 990 filings. No active RFP confirmed.",
        "days_to_deadline": 365, "deadline": dl(365),
        "eligibility": "CRA-qualified affordable housing programs. Inferred from historical data. Verify before outreach.",
        "award_label": "$25,000 – $100,000 (historical median)", "award_min": 25000, "award_max": 100000,
        "application_method": "CRA officer outreach — relationship building recommended", "application_url": "#",
        "disqualifying_restrictions": "Archival data only — current funding availability unconfirmed.",
        "required_documents": ["N/A — relationship-building phase"],
        "match_score": 3.2, "retrieved_ago": "just now",
        "scoring": {
            "geographic_alignment": {"score": 5, "label": "Geographic Alignment", "explanation": "Chicago metro CRA lender; geographic fit ideal."},
            "population_served": {"score": 3, "label": "Population Served", "explanation": "General affordable housing focus; women-specific programs within scope."},
            "budget_fit": {"score": 3, "label": "Budget Fit", "explanation": "Historical median award fits budget; CRA funds typically smaller.", "is_highest_weight": True},
            "timeline_feasibility": {"score": 2, "label": "Timeline Feasibility", "explanation": "No active RFP. Relationship cultivation required."},
        },
        "location": "Chicago, IL",
    }

def show_dashboard():
    org = st.session_state.org_profile
    st.markdown("# Grant Pipeline")
    st.caption(f"Opportunities surfaced for **{org['name']}**, ranked by fit.")

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
            for alert in st.session_state.alert_log:
                st.markdown(f"**{alert['grant_name']}**")
                st.caption(f"{alert['funding_org']} · Sent to {alert['recipients']} recipients · {alert['sent_ago']}")
                st.divider()

    # Grant detail
    show_grant_detail()

    # Controls
    cc1, cc2, cc3, cc4 = st.columns([2, 2, 2, 1])
    with cc1:
        sort_by = st.selectbox("Sort", SORT_OPTIONS, index=SORT_OPTIONS.index(st.session_state.sort_by), label_visibility="collapsed", key="sort_sel")
        st.session_state.sort_by = sort_by

    with cc2:
        if st.button("🗄️ Search Archival (990)", use_container_width=True):
            existing_keys = {g["funding_org"] + g["program_name"] for g in st.session_state.grants}
            new_arc = _make_archival_grant()
            with st.status("Scanning 990 filings...", expanded=True) as status:
                st.write("Searching IRS Form 990 records...")
                time.sleep(1.2)
                st.write("Identifying historical giving patterns...")
                time.sleep(1.2)
                st.write("Scoring and ranking archival leads...")
                time.sleep(0.8)
                status.update(label="Archival scan complete!", state="complete")
            if new_arc["funding_org"] + new_arc["program_name"] not in existing_keys:
                st.session_state.grants.append(new_arc)
                alert = {"id": f"a{random.randint(100,999)}", "grant_name": "Archival scan complete", "funding_org": "Wintrust Community Advantage added to archival sources", "sent_ago": "just now", "recipients": len(st.session_state.users)}
                st.session_state.alert_log.insert(0, alert)
                st.session_state.unread_alerts += 1
            st.rerun()

    with cc3:
        if st.button("▶ Run Grant Search", use_container_width=True, type="primary"):
            existing_keys = {g["funding_org"] + g["program_name"] for g in st.session_state.grants}
            new_grants = _make_agent_grants()
            with st.status("Running grant search...", expanded=True) as status:
                st.write("🔵 Scanning sources: Grants.gov, Instrumentl, PND, GrantStation...")
                time.sleep(1.2)
                st.write("🟣 Matching to organization profile...")
                time.sleep(1.2)
                st.write("🟢 Ranking and filtering results...")
                time.sleep(1.2)
                st.write("✅ Finalizing new leads...")
                time.sleep(0.4)
                status.update(label="Grant search complete! New leads added.", state="complete")
            added = 0
            for g in new_grants:
                if g["funding_org"] + g["program_name"] not in existing_keys:
                    st.session_state.grants.append(g)
                    added += 1
            if added:
                alert = {"id": f"a{random.randint(100,999)}", "grant_name": f"Agent found {added} new lead{'s' if added > 1 else ''}", "funding_org": "Driehaus Foundation · Woods Fund Chicago · Steans Family Foundation", "sent_ago": "just now", "recipients": len(st.session_state.users)}
                st.session_state.alert_log.insert(0, alert)
                st.session_state.unread_alerts += 1
            st.rerun()

    with cc4:
        if st.button("+ Add Grant", use_container_width=True):
            st.session_state.page = "add_grant"
            st.rerun()

    # Partition
    all_g = st.session_state.grants
    hot = sort_grants([g for g in all_g if g["temperature"] == "hot" and not is_cold_lead(g)], sort_by)
    warm = sort_grants([g for g in all_g if g["temperature"] == "warm" and not is_cold_lead(g)], sort_by)
    archival = sort_grants([g for g in all_g if g["temperature"] == "less-warm"], sort_by)
    cold = [g for g in all_g if is_cold_lead(g) and not g.get("is_manually_added")]
    visible = hot + warm
    avg_score = sum(g["match_score"] for g in visible) / max(len(visible), 1)

    # Stats
    st.divider()
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("🔴 Hot Leads", len(hot))
    s2.metric("🟡 Warm Leads", len(warm))
    s3.metric("⚪ Archival Sources", len(archival))
    s4.metric("📊 Avg Match Score", f"{avg_score:.1f}")
    st.divider()

    # Tabs
    tab_hot, tab_warm = st.tabs([f"🔴 Act Now ({len(hot)})", f"🟡 Coming Up ({len(warm)})"])
    with tab_hot:
        st.caption("Hot leads with imminent deadlines or open application paths. Get these out the door first.")
        if hot:
            for g in hot:
                show_grant_card(g)
        else:
            st.info("No hot leads right now. Click **▶ Run Grant Search** to find new opportunities.")

    with tab_warm:
        st.caption("Warm leads queued for the next grant cycle. Funders likely to open applications soon.")
        if warm:
            for g in warm:
                show_grant_card(g)
        else:
            st.info("No warm leads queued. Click **▶ Run Grant Search** to surface upcoming opportunities.")

    # Archival
    st.divider()
    with st.expander(f"⚪ Archival Sources — 990 Data  ({len(archival)} sources)", expanded=False):
        st.caption("Historical giving patterns from IRS Form 990 filings. No active RFP confirmed. Use for cultivation planning.")
        if archival:
            for g in archival:
                show_grant_card(g)
        else:
            st.info("No archival sources yet. Click **🗄️ Search Archival (990)** to scan 990 filings.")

    # Cold leads notice
    if cold:
        st.caption(f"🙈 **{len(cold)} cold lead{'s' if len(cold)>1 else ''} hidden** — require existing relationships or do not accept unsolicited applications.")

# ── Organization Profile ──────────────────────────────────────────────────────

def show_profile():
    st.markdown("# Organization Profile")
    st.caption("This profile powers the AI matching engine. Keep it current for the best grant recommendations.")
    org = st.session_state.org_profile

    with st.form("profile_form"):
        org_name = st.text_input("Organization Name *", value=org["name"])
        mission = st.text_area("Mission Statement *", value=org["mission"], height=100)
        funding_needs = st.text_area("Funding Needs", value=org["funding_needs"], height=80)

        st.markdown("**Program Areas**")
        all_options = PRESET_AREAS + [a for a in org["program_areas"] if a not in PRESET_AREAS]
        selected_areas = st.multiselect("Program Areas", options=all_options, default=org["program_areas"], label_visibility="collapsed")
        custom_area = st.text_input("Add custom program area", placeholder="Type and press Save")

        c1, c2 = st.columns(2)
        with c1:
            loc_idx = LOCATIONS.index(org["location_focus"]) if org["location_focus"] in LOCATIONS else 0
            location_focus = st.selectbox("Geographic Focus *", LOCATIONS, index=loc_idx)
        with c2:
            bud_idx = BUDGET_RANGES.index(org["budget_range"]) if org["budget_range"] in BUDGET_RANGES else 6
            budget_range = st.selectbox("Annual Budget Range *", BUDGET_RANGES, index=bud_idx)

        populations = st.text_area("Populations Served", value=org["populations_served"], height=80)

        saved = st.form_submit_button("💾 Save Profile", type="primary")

    if saved:
        areas = list(selected_areas)
        if custom_area.strip() and custom_area.strip() not in areas:
            areas.append(custom_area.strip())
        st.session_state.org_profile = {
            "name": org_name, "mission": mission, "funding_needs": funding_needs,
            "program_areas": areas, "location_focus": location_focus,
            "budget_range": budget_range, "populations_served": populations,
        }
        st.success("✅ Profile saved! Grant matching will use your updated profile.")

# ── Add Grant ─────────────────────────────────────────────────────────────────

def show_add_grant():
    st.markdown("# Add Grant Manually")
    st.caption("Useful for board referrals, network leads, or funders you track offline. Manually added grants bypass cold-lead filtering.")

    with st.form("add_grant_form"):
        c1, c2 = st.columns(2)
        with c1:
            funding_org = st.text_input("Funding Organization *")
            program_name = st.text_input("Program / Grant Name *")
            award_label = st.text_input("Award Range *", placeholder="e.g. $50,000 – $150,000")
            deadline_str = st.text_input("Deadline *", placeholder="e.g. Aug 15, 2026")
            award_min = st.number_input("Award Min ($)", min_value=0, value=0, step=5000)
        with c2:
            source = st.selectbox("Source", ["Manual Entry", "Philanthropy News Digest", "Instrumentl", "Grants.gov", "GrantStation", "Zeffy", "Archival 990"])
            source_url = st.text_input("Source URL", placeholder="https://... (where you found this grant)")
            application_method = st.text_input("Application Method", placeholder="e.g. Online portal")
            program_contact = st.text_input("Program Contact")
            award_max = st.number_input("Award Max ($)", min_value=0, value=50000, step=5000)
            temperature = st.selectbox("Lead Temperature", ["hot", "warm", "less-warm"], format_func=lambda t: {"hot": "🔴 Hot", "warm": "🟡 Warm", "less-warm": "⚪ Archival"}.get(t, t))

        description = st.text_area("Description *", height=100)
        eligibility = st.text_area("Eligibility", height=60)
        disqualifying = st.text_area("Disqualifying Restrictions", height=60)

        submitted = st.form_submit_button("Add Grant to Pipeline", type="primary")

    if submitted:
        if not funding_org or not program_name or not description or not award_label or not deadline_str:
            st.error("Please fill in all required fields (marked with *).")
        else:
            st.session_state.grants.append({
                "id": f"m-{random.randint(1000, 9999)}", "temperature": temperature,
                "source": source, "source_url": source_url, "is_manually_added": True,
                "funding_org": funding_org, "program_name": program_name,
                "program_contact": program_contact, "description": description,
                "days_to_deadline": 30, "deadline": deadline_str,
                "eligibility": eligibility, "award_label": award_label,
                "award_min": award_min, "award_max": award_max,
                "application_method": application_method, "application_url": "#",
                "disqualifying_restrictions": disqualifying, "required_documents": [],
                "match_score": 3.5, "retrieved_ago": "just now",
                "scoring": {
                    "geographic_alignment": {"score": 3, "label": "Geographic Alignment", "explanation": "Manually added — verify geographic eligibility."},
                    "population_served": {"score": 3, "label": "Population Served", "explanation": "Manually added — verify population fit."},
                    "budget_fit": {"score": 3, "label": "Budget Fit", "explanation": "Manually added — verify award range alignment.", "is_highest_weight": True},
                    "timeline_feasibility": {"score": 3, "label": "Timeline Feasibility", "explanation": "Manually added — verify timeline feasibility."},
                },
                "location": "Chicago, IL",
            })
            st.success(f"✅ '{program_name}' added to your pipeline.")
            st.session_state.page = "dashboard"
            st.rerun()

# ── User Management ───────────────────────────────────────────────────────────

def show_users():
    if st.session_state.current_user.get("role") != "admin":
        st.error("🔒 User Management is restricted to Admin accounts. Use the Demo role switcher in the sidebar to preview this view.")
        return

    st.markdown("# User Management")
    st.caption("Add and remove team members and assign roles. Approve or deny pending sign-up requests.")

    current_id = st.session_state.current_user["id"]

    # Pending approvals
    pending = st.session_state.pending_users
    if pending:
        st.subheader(f"⏳ Pending Approvals ({len(pending)})")
        st.caption("These users signed up and are awaiting your approval before they can access the platform.")
        for user in pending:
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
                with c1:
                    st.markdown(f"**{user['name']}**")
                    st.caption(user["email"])
                with c2:
                    st.caption(user.get("organization", "—"))
                    st.caption(user.get("title", "—"))
                with c3:
                    st.caption(f"Role: {user['role'].title()}")
                    if st.button("✅ Approve", key=f"approve_{user['id']}", use_container_width=True):
                        st.session_state.users.append({**user, "status": "approved"})
                        st.session_state.pending_users = [u for u in pending if u["id"] != user["id"]]
                        st.success(f"{user['name']} approved!")
                        st.rerun()
                with c4:
                    st.write("")
                    if st.button("✕ Deny", key=f"deny_{user['id']}", use_container_width=True):
                        st.session_state.pending_users = [u for u in pending if u["id"] != user["id"]]
                        st.rerun()
        st.divider()

    # Add user directly
    st.subheader("Add User Directly")
    with st.form("add_user_form"):
        au1, au2, au3 = st.columns([2, 2, 1])
        with au1:
            new_name = st.text_input("Full Name")
        with au2:
            new_email = st.text_input("Email")
        with au3:
            new_role_label = st.selectbox("Role", ["Basic User", "Admin"])
        if st.form_submit_button("Add User", type="primary"):
            if not new_name or not new_email:
                st.error("Please fill in name and email.")
            else:
                st.session_state.users.append({
                    "id": f"u{random.randint(1000, 9999)}",
                    "name": new_name, "email": new_email,
                    "role": "admin" if new_role_label == "Admin" else "basic",
                    "status": "approved",
                })
                st.success(f"Invitation sent to {new_email} (simulated).")
                st.rerun()

    # Active team
    st.subheader(f"Active Team ({len(st.session_state.users)})")
    for user in st.session_state.users:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 2, 1, 0.5])
            with c1:
                label = f"**{user['name']}**"
                if user["id"] == current_id:
                    label += "  *(You)*"
                st.markdown(label)
                st.caption(user["email"])
            with c2:
                st.caption(user.get("organization", "—"))
                st.caption(user.get("title", "—"))
            with c3:
                if user["id"] != current_id:
                    roles = ["Admin", "Basic User"]
                    cur = 0 if user["role"] == "admin" else 1
                    sel = st.selectbox("Role", roles, index=cur, key=f"role_{user['id']}", label_visibility="collapsed")
                    new_r = "admin" if sel == "Admin" else "basic"
                    if new_r != user["role"]:
                        for u in st.session_state.users:
                            if u["id"] == user["id"]:
                                u["role"] = new_r
                        st.rerun()
                else:
                    st.caption("Admin" if user["role"] == "admin" else "Basic User")
            with c4:
                st.write("")
                if user["id"] != current_id:
                    if st.button("🗑️", key=f"del_{user['id']}", help="Remove user"):
                        st.session_state.users = [u for u in st.session_state.users if u["id"] != user["id"]]
                        st.rerun()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_state()

    if not st.session_state.current_user:
        show_login()
        return

    show_sidebar()

    page = st.session_state.page
    if page == "dashboard":
        show_dashboard()
    elif page == "profile":
        show_profile()
    elif page == "add_grant":
        show_add_grant()
    elif page == "users":
        show_users()

main()
