"""
frontend/prospects.py
---------------------
Phase 1b — Prospects page + Pipeline page.

  render_prospects(api, user) — the working surface for grant writers:
      compact rows of opportunities, click to expand into the
      conversational narrative + Pursue/Watch/Pass actions + scored
      breakdown.

  render_pipeline(api, user) — three-column view of Pursuing / Watching /
      Passed with deadline-conflict warnings + a "Generate weekly digest"
      button at the top.

Narratives are fetched lazily on expand. Each opportunity tracks its
own "expanded" + "narrative_loaded" state in st.session_state under a
prefix keyed by opp_key.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import streamlit as st

from frontend.api import APIError, GProspectAPI


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

# Color codes for the deadline urgency badge — used in compact rows and cards.
BUCKET_BADGE = {
    "hot":     ("🔥", "Hot — act this week"),
    "warm":    ("⚠️", "Warm — start cultivation soon"),
    "cold":    ("🧊", "Cold — monitor / backlog"),
    "past":    ("⏰", "Past — deadline already closed"),
    "unknown": ("❔", "No deadline available"),
}

PURSUIT_BADGE = {
    "pursuing": "✅ Pursuing",
    "watching": "👀 Watching",
    "passed":   "🚫 Passed",
}


def _badge(opp: dict) -> str:
    bucket = opp.get("deadline_bucket") or "unknown"
    icon, _ = BUCKET_BADGE.get(bucket, BUCKET_BADGE["unknown"])
    return icon


def _pursuit_label(opp: dict) -> str:
    p = opp.get("pursuit")
    if not p:
        return ""
    status = p.get("status")
    return PURSUIT_BADGE.get(status, "")


# ─────────────────────────────────────────────────────────────────────────────
# Prospects page
# ─────────────────────────────────────────────────────────────────────────────

def render_prospects(api: GProspectAPI, user: dict) -> None:
    st.title("🎯 Prospects")
    st.caption(
        "Ranked grant opportunities matched against your profile. Click a row "
        "to read the AI 'why this fits' narrative, mark it as Pursuing / "
        "Watching / Passed, or see the scored breakdown."
    )

    # ── Filter bar ────────────────────────────────────────────────────────────
    fcol1, fcol2, fcol3, fcol4 = st.columns([2, 2, 2, 1])
    min_score = fcol1.slider(
        "Min score", min_value=0.0, max_value=5.0, value=0.0, step=0.25,
    )
    deadline_window = fcol2.selectbox(
        "Deadline window",
        options = ["All", "Next 30 days", "Next 60 days"],
        index   = 0,
    )
    pursuit_filter = fcol3.selectbox(
        "Pipeline status",
        options = ["All", "New", "Pursuing", "Watching", "Passed"],
        index   = 0,
    )
    if fcol4.button("↻ Refresh", use_container_width=True):
        # Drop the narrative cache hints in session_state so expansions re-fetch.
        for k in list(st.session_state.keys()):
            if k.startswith("narrative_"):
                del st.session_state[k]

    # ── Fetch ─────────────────────────────────────────────────────────────────
    api_pursuit = {
        "All":      None,
        "New":      "new",
        "Pursuing": "pursuing",
        "Watching": "watching",
        "Passed":   "passed",
    }[pursuit_filter]

    try:
        opportunities = api.list_opportunities(
            limit     = 200,
            min_score = min_score if min_score > 0 else None,
            pursuit   = api_pursuit,
        )
    except APIError as e:
        st.error(f"Could not load opportunities: {e.detail}")
        return

    # Deadline-window filter (client-side — keeps server API simple)
    if deadline_window == "Next 30 days":
        opportunities = [
            o for o in opportunities
            if o.get("days_remaining") is not None and 0 <= o["days_remaining"] <= 30
        ]
    elif deadline_window == "Next 60 days":
        opportunities = [
            o for o in opportunities
            if o.get("days_remaining") is not None and 0 <= o["days_remaining"] <= 60
        ]

    if not opportunities:
        st.info(
            "No matching opportunities. Either no agent run has produced "
            "results yet (an admin can trigger via `POST /results/run`), or "
            "the filters are too tight. Try widening the score floor or "
            "deadline window."
        )
        return

    st.caption(f"Showing {len(opportunities)} opportunit"
               f"{'y' if len(opportunities) == 1 else 'ies'}.")
    st.write("")

    # ── Compact rows ──────────────────────────────────────────────────────────
    for opp in opportunities:
        _render_opportunity_row(api, opp)


def _render_opportunity_row(api: GProspectAPI, opp: dict) -> None:
    """One compact-row + expander pair."""
    badge        = _badge(opp)
    pursuit_lbl  = _pursuit_label(opp)
    score        = opp.get("score_final")
    score_str    = f"{score:.2f}" if score is not None else "—"
    deadline_str = opp.get("application_deadline") or "no deadline"

    label = (
        f"**{badge} #{opp['rank']}**  ·  **{opp['funder_name']}** — "
        f"{opp['program_name']}  ·  Score **{score_str}**  ·  "
        f"{deadline_str}  ·  {pursuit_lbl}".strip(" ·")
    )
    with st.expander(label, expanded=False):
        _render_opportunity_expanded(api, opp)


def _render_opportunity_expanded(api: GProspectAPI, opp: dict) -> None:
    """Inside the expander — the full card."""
    opp_key   = opp["opp_key"]
    funder    = opp["funder_name"]
    program   = opp["program_name"]

    # ── Header meta ───────────────────────────────────────────────────────────
    meta_cols = st.columns(4)
    meta_cols[0].metric("Score",      f"{opp['score_final']:.2f}" if opp.get("score_final") is not None else "—")
    meta_cols[1].metric("Days left",  opp.get("days_remaining") if opp.get("days_remaining") is not None else "—")
    meta_cols[2].metric("Award range", opp.get("award_range") or "—")
    bucket = opp.get("deadline_bucket") or "unknown"
    bucket_icon, bucket_help = BUCKET_BADGE.get(bucket, BUCKET_BADGE["unknown"])
    meta_cols[3].metric("Urgency",    f"{bucket_icon} {bucket.title()}", help=bucket_help)

    # ── Conversational narrative (lazy fetch) ─────────────────────────────────
    narrative_key = f"narrative_{opp_key}"
    st.markdown("##### Why this fits")
    if narrative_key in st.session_state:
        narrative = st.session_state[narrative_key]
        st.markdown(narrative["conversational_md"])
        with st.expander("Show scored breakdown"):
            _render_scored_breakdown(narrative.get("scored_breakdown") or {})
        st.caption(
            f"Source: {'cached' if narrative.get('cached') else 'just generated'} "
            f"· profile v{narrative.get('profile_version')}"
        )
    else:
        c1, c2 = st.columns([1, 5])
        if c1.button("Generate", key=f"gen_{opp_key}", type="primary"):
            with st.spinner("Asking Claude…"):
                try:
                    narrative = api.get_or_generate_narrative(opp_key)
                except APIError as e:
                    st.error(f"Couldn't generate narrative: {e.detail}")
                    return
            st.session_state[narrative_key] = narrative
            st.rerun()
        c2.caption(
            "Narratives are generated on demand. First click → ~one Claude "
            "call + cache; subsequent loads are instant."
        )

    st.write("")

    # ── Pursuit actions ───────────────────────────────────────────────────────
    st.markdown("##### Pipeline action")
    current_status = (opp.get("pursuit") or {}).get("status")
    pcols = st.columns(4)
    if pcols[0].button(
        "Pursue", key=f"pursue_{opp_key}",
        type = "primary" if current_status == "pursuing" else "secondary",
        use_container_width = True,
    ):
        _do_pursuit(api, opp_key, "pursue")
    if pcols[1].button(
        "Watch", key=f"watch_{opp_key}",
        type = "primary" if current_status == "watching" else "secondary",
        use_container_width = True,
    ):
        _do_pursuit(api, opp_key, "watch")
    if pcols[2].button(
        "Pass", key=f"pass_{opp_key}",
        type = "primary" if current_status == "passed" else "secondary",
        use_container_width = True,
    ):
        _do_pursuit(api, opp_key, "pass")
    if current_status:
        if pcols[3].button("Clear", key=f"clear_{opp_key}", use_container_width=True):
            _do_pursuit(api, opp_key, "clear")

    # ── Apply link ────────────────────────────────────────────────────────────
    if opp.get("application_url"):
        st.markdown(
            f"**Apply / details:** [{opp['application_url']}]({opp['application_url']})"
        )

    if opp.get("next_action"):
        st.caption(f"**Next action:** {opp['next_action']}")


def _render_scored_breakdown(scored: dict) -> None:
    """Render the per-dimension scores as a small table."""
    if not scored:
        st.caption("No scored breakdown available.")
        return
    rows = []
    for dim, info in scored.items():
        if isinstance(info, dict):
            rows.append({
                "Dimension": dim.title(),
                "Score":     f"{info.get('score', 0):.2f}",
                "Reason":    info.get("reason", ""),
            })
    if rows:
        st.dataframe(
            pd.DataFrame(rows),
            hide_index          = True,
            use_container_width = True,
        )


def _do_pursuit(api: GProspectAPI, opp_key: str, action: str) -> None:
    try:
        api.set_pursuit(opp_key, action)
    except APIError as e:
        st.error(f"Action failed: {e.detail}")
        return
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline page
# ─────────────────────────────────────────────────────────────────────────────

def render_pipeline(api: GProspectAPI, user: dict) -> None:
    st.title("📋 Pipeline")
    st.caption(
        "Active pursuits, things you're watching, and recent passes. "
        "Generate the weekly digest here when you're ready to circulate."
    )

    # ── Digest button ─────────────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown("**Weekly digest**")
        st.caption(
            "Top 5 opportunities + 30-day deadline calendar, both PDF and "
            "DOCX. Uses already-cached narratives — no fresh Claude calls."
        )
        if st.button("📥 Generate digest", type="primary"):
            with st.spinner("Building digest…"):
                try:
                    zip_bytes = api.generate_digest_zip()
                except APIError as e:
                    st.error(f"Could not generate digest: {e.detail}")
                    return
            st.session_state["digest_zip_bytes"] = zip_bytes
            st.success("Digest ready — download below.")
        if "digest_zip_bytes" in st.session_state:
            st.download_button(
                label     = "⬇️ Download weekly_digest.zip",
                data      = st.session_state["digest_zip_bytes"],
                file_name = "gprospect_weekly_digest.zip",
                mime      = "application/zip",
            )

    st.write("")

    # ── Fetch all opportunities ───────────────────────────────────────────────
    try:
        opps = api.list_opportunities(limit=500)
    except APIError as e:
        st.error(f"Could not load opportunities: {e.detail}")
        return

    pursuing = [o for o in opps if (o.get("pursuit") or {}).get("status") == "pursuing"]
    watching = [o for o in opps if (o.get("pursuit") or {}).get("status") == "watching"]
    passed   = [o for o in opps if (o.get("pursuit") or {}).get("status") == "passed"]

    # ── Deadline-conflict warnings (≥2 pursuing opps with deadlines within 7d)
    conflicts = _detect_deadline_conflicts(pursuing)
    if conflicts:
        with st.container(border=True):
            st.warning("⚠️ **Deadline conflicts in active pursuits**")
            for a, b, gap in conflicts:
                st.markdown(
                    f"- **{a['funder_name']} — {a['program_name']}** "
                    f"({a.get('application_deadline')}) and "
                    f"**{b['funder_name']} — {b['program_name']}** "
                    f"({b.get('application_deadline')}) close within {gap} day(s)."
                )

    # ── Three columns ─────────────────────────────────────────────────────────
    cols = st.columns(3)
    _render_pipeline_column(cols[0], "✅ Pursuing", pursuing)
    _render_pipeline_column(cols[1], "👀 Watching", watching)
    _render_pipeline_column(cols[2], "🚫 Passed",   passed)


def _render_pipeline_column(col, title: str, opps: list[dict]) -> None:
    with col:
        st.subheader(title)
        st.caption(f"{len(opps)} opportunit{'y' if len(opps) == 1 else 'ies'}")
        if not opps:
            st.caption("_(empty)_")
            return
        for o in opps:
            with st.container(border=True):
                st.markdown(f"**{o['funder_name']}**")
                st.caption(o['program_name'])
                badge = _badge(o)
                meta = []
                if o.get("score_final") is not None:
                    meta.append(f"Score {o['score_final']:.2f}")
                if o.get("application_deadline"):
                    meta.append(f"{badge} {o['application_deadline']}")
                st.caption(" · ".join(meta))


def _detect_deadline_conflicts(pursuing: list[dict],
                               window_days: int = 7) -> list[tuple[dict, dict, int]]:
    """
    Returns pairs of pursuing opportunities whose deadlines are within
    `window_days` of each other.
    """
    dated = [
        o for o in pursuing
        if o.get("days_remaining") is not None and o["days_remaining"] >= 0
    ]
    dated.sort(key=lambda o: o["days_remaining"])
    out: list[tuple[dict, dict, int]] = []
    for i in range(len(dated)):
        for j in range(i + 1, len(dated)):
            gap = dated[j]["days_remaining"] - dated[i]["days_remaining"]
            if gap <= window_days:
                out.append((dated[i], dated[j], gap))
            else:
                break   # sorted; subsequent gaps only grow
    return out
