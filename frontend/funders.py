"""
frontend/funders.py
-------------------
Phase 2 — Funders tab.

  render_funders(api, user)
    Single page with:
      - "Run discovery now" trigger
      - Status filter + candidate count summary
      - Compact-row candidate list
      - Per-row expander: rationale, structured signals, 5-year
        trajectory mini-chart, status buttons
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import streamlit as st

from frontend.api import APIError, GProspectAPI


STATUS_BADGE = {
    "candidate": "🆕 Candidate",
    "watching":  "👀 Watching",
    "engaged":   "🤝 Engaged",
    "dismissed": "🚫 Dismissed",
}

STATUS_FILTERS = [
    ("Active",     None),         # hides dismissed
    ("Candidates", "candidate"),
    ("Watching",   "watching"),
    ("Engaged",    "engaged"),
    ("Dismissed",  "dismissed"),
    ("All",        "all"),
]


def render_funders(api: GProspectAPI, user: dict) -> None:
    st.title("🏛️ Funders")
    st.caption(
        "Foundations the discovery cycle has surfaced as plausible matches for "
        "your profile. Each candidate is scored on geography, foundation type, "
        "size, and filing recency. Mark them Watching when interesting, "
        "Engaged when actively cultivating, Dismissed to stop suggesting."
    )

    # ── Trigger row ───────────────────────────────────────────────────────────
    with st.container(border=True):
        c1, c2, c3 = st.columns([2, 2, 4])
        if c1.button("🔍 Run discovery now", type="primary",
                     use_container_width=True):
            try:
                resp = api.trigger_discovery_run()
            except APIError as e:
                st.error(f"Could not start discovery: {e.detail}")
            else:
                st.success(resp.get("message") or "Discovery cycle queued.")
        if c2.button("📥 Ingest 990 data", use_container_width=True):
            try:
                resp = api.trigger_grants_ingest()
            except APIError as e:
                st.error(f"Could not start ingestion: {e.detail}")
            else:
                st.success(resp.get("message") or "Ingestion queued.")
        c3.caption(
            "Discovery searches ProPublica for in-state foundations matching "
            "your NTEE focus. Ingest pulls Schedule I grant detail from IRS "
            "990 e-file data — this unlocks the **warm paths** below. "
            "Re-run discovery weekly; ingest monthly."
        )

    # ── Pull warm-path summary once for badge rendering ──────────────────────
    try:
        warm_summary = api.get_warm_path_summary()
    except APIError as e:
        st.warning(f"Couldn't load warm-path summary: {e.detail}")
        warm_summary = []
    warm_by_ein = {row["funder_ein"]: row for row in (warm_summary or [])}

    # ── Filter ────────────────────────────────────────────────────────────────
    f_label_to_value = dict(STATUS_FILTERS)
    f_label = st.selectbox(
        "Filter",
        options = [lbl for lbl, _ in STATUS_FILTERS],
        index   = 0,
    )
    api_filter = f_label_to_value[f_label]

    try:
        candidates = api.list_funder_candidates(status_filter=api_filter)
    except APIError as e:
        st.error(f"Could not load candidates: {e.detail}")
        return

    if not candidates:
        st.info(
            "No candidates yet. Click **Run discovery now** above to populate "
            "your funder pool. The first run takes about a minute. "
            "If you've already run discovery and nothing shows, your filter "
            "may be too narrow — try **Active** or **All**."
        )
        return

    st.caption(f"Showing {len(candidates)} candidate"
               f"{'' if len(candidates) == 1 else 's'}.")
    st.write("")

    for cand in candidates:
        warm = warm_by_ein.get(cand.get("ein"))
        _render_candidate_row(api, cand, warm=warm)


def _render_candidate_row(api: GProspectAPI, cand: dict,
                           warm: Optional[dict] = None) -> None:
    status_label = STATUS_BADGE.get(cand.get("status"), "")
    state        = cand.get("funder_state") or ""
    ntee         = cand.get("ntee_code") or ""
    score        = cand.get("score") or 0.0

    warm_badge = ""
    if warm and (warm.get("peer_grant_count") or 0) > 0:
        n = warm["peer_grant_count"]
        warm_badge = f"  ·  🤝 **{n} warm path{'' if n == 1 else 's'}**"

    label = (
        f"**{cand['funder_name']}**  ·  "
        f"Score **{score:.2f}**  ·  "
        f"{state} {ntee}  ·  {status_label}{warm_badge}"
    ).strip(" ·")
    with st.expander(label, expanded=False):
        _render_candidate_expanded(api, cand, warm=warm)


def _render_candidate_expanded(api: GProspectAPI, cand: dict,
                                warm: Optional[dict] = None) -> None:
    ein     = cand.get("ein")
    signals = cand.get("signals") or {}

    # ── Rationale ─────────────────────────────────────────────────────────────
    st.markdown("##### Why surfaced")
    st.markdown(cand.get("rationale") or "_(no rationale captured)_")

    # ── Warm paths ────────────────────────────────────────────────────────────
    _render_warm_paths(api, ein, warm)

    # ── Structured signals ────────────────────────────────────────────────────
    with st.expander("Structured signals", expanded=False):
        if signals:
            sig_df = pd.DataFrame(
                [{"Signal": k, "Value": str(v)} for k, v in signals.items()]
            )
            st.dataframe(sig_df, hide_index=True, use_container_width=True)
        else:
            st.caption("No structured signals captured.")

    # ── Live ProPublica detail (financial trajectory) ────────────────────────
    st.markdown("##### Recent filings")
    if not ein:
        st.caption("Missing EIN — can't fetch live filings.")
    else:
        detail_key = f"funder_detail_{ein}"
        if detail_key not in st.session_state:
            if st.button("Load 5-year financial trajectory",
                         key=f"loadtraj_{ein}"):
                with st.spinner("Querying ProPublica…"):
                    try:
                        detail = api.get_funder_detail(ein)
                    except APIError as e:
                        st.error(f"Couldn't load details: {e.detail}")
                        return
                st.session_state[detail_key] = detail
                st.rerun()
            else:
                st.caption(
                    "Click above to fetch ProPublica's 5-year financial "
                    "trajectory + filing links. Costs one polite API call "
                    "per click."
                )
        else:
            _render_propublica_detail(st.session_state[detail_key])

    # ── Status buttons ────────────────────────────────────────────────────────
    st.markdown("##### Pipeline action")
    current = cand.get("status")
    cols = st.columns(4)
    _status_button(cols[0], api, ein, "watching",  "Watch",   current)
    _status_button(cols[1], api, ein, "engaged",   "Engage",  current)
    _status_button(cols[2], api, ein, "dismissed", "Dismiss", current)
    if current and current != "candidate":
        if cols[3].button("Reset to Candidate", key=f"reset_{ein}",
                          use_container_width=True):
            _set_status_and_rerun(api, ein, "candidate")


def _status_button(col, api, ein, target_status, label, current_status):
    active = current_status == target_status
    if col.button(
        label, key=f"{target_status}_{ein}",
        type = "primary" if active else "secondary",
        use_container_width = True,
    ):
        _set_status_and_rerun(api, ein, target_status)


def _set_status_and_rerun(api: GProspectAPI, ein: str, target: str) -> None:
    try:
        api.set_candidate_status(ein, target)
    except APIError as e:
        st.error(f"Could not update status: {e.detail}")
        return
    st.rerun()


def _render_propublica_detail(detail: dict) -> None:
    """Render the live ProPublica payload — org card + filings table."""
    if detail.get("error"):
        st.warning(f"ProPublica fetch failed: {detail['error']}")
        return

    org     = detail.get("propublica", {}).get("organization") or {}
    filings = detail.get("propublica", {}).get("filings")     or []

    if org:
        addr_bits = [b for b in (org.get("city"), org.get("state"), org.get("zipcode")) if b]
        if addr_bits:
            st.caption(f"📍 {', '.join(addr_bits)}")
        if org.get("guidestar_url"):
            st.markdown(f"[GuideStar profile]({org['guidestar_url']})  ·  "
                        f"[NCCS profile]({org.get('nccs_url') or '#'})")

    if not filings:
        st.caption("No filings with financial data available from ProPublica.")
        return

    # Build a small chart-friendly table
    rows = []
    for f in filings:
        rows.append({
            "Tax year":   f.get("tax_prd_yr"),
            "Revenue":    _fmt_usd(f.get("totrevenue")),
            "Expenses":   _fmt_usd(f.get("totfuncexpns")),
            "Assets":     _fmt_usd(f.get("totassetsend")),
            "Liabilities":_fmt_usd(f.get("totliabend")),
            "Form":       _form_label(f.get("formtype")),
            "PDF":        f.get("pdf_url") or "",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, use_container_width=True)

    # Tiny trajectory chart — assets over time (most stable signal for a foundation)
    chart_df = pd.DataFrame([
        {"Year": f.get("tax_prd_yr"), "Assets": f.get("totassetsend") or 0}
        for f in filings
        if f.get("tax_prd_yr")
    ])
    if not chart_df.empty:
        chart_df = chart_df.sort_values("Year")
        st.caption("Total assets, end of year")
        st.line_chart(chart_df.set_index("Year")["Assets"])


def _render_warm_paths(api: GProspectAPI, ein: Optional[str],
                        warm_summary: Optional[dict]) -> None:
    """
    Render the warm-paths section: how many peer grants this funder has
    given + the inline list of peer recipients.

    Cheap path: if the per-funder summary already says 0 peer grants,
    show the "no warm paths" hint without fetching the detail list.
    """
    st.markdown("##### Warm paths")
    if not ein:
        st.caption("Missing EIN — can't look up warm paths.")
        return

    if warm_summary is not None and (warm_summary.get("peer_grant_count") or 0) == 0:
        st.caption(
            "No grants ingested for this funder yet, or none to peers in your "
            "state. Try **Ingest 990 data** above to pull Schedule I from the "
            "IRS, then re-open this card."
        )
        return

    detail_key = f"warm_paths_{ein}"
    if detail_key not in st.session_state:
        if st.button("Load peer grants", key=f"loadwarm_{ein}"):
            try:
                resp = api.get_warm_paths_for_funder(ein)
            except APIError as e:
                st.error(f"Couldn't load warm paths: {e.detail}")
                return
            st.session_state[detail_key] = resp
            st.rerun()
        else:
            n = (warm_summary or {}).get("peer_grant_count") or 0
            st.caption(
                f"{n} peer grant{'' if n == 1 else 's'} on record. "
                f"Click above to load the recipient list."
            )
        return

    resp        = st.session_state[detail_key]
    peer_grants = resp.get("peer_grants") or []
    note        = resp.get("note")
    if note:
        st.caption(note)
    if not peer_grants:
        st.caption("No peer grants found for this funder.")
        return

    rows = []
    for g in peer_grants:
        rows.append({
            "Recipient":   g["recipient_name"],
            "Location":    " ".join(
                bit for bit in (g.get("recipient_city"), g.get("recipient_state"))
                if bit
            ),
            "Year":        g.get("fiscal_year") or "—",
            "Amount":      _fmt_usd(g.get("amount")),
            "Why peer":    ", ".join(g.get("reasons") or []),
        })
    st.dataframe(
        pd.DataFrame(rows),
        hide_index          = True,
        use_container_width = True,
    )


def _fmt_usd(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"${int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def _form_label(code) -> str:
    return {0: "990", 1: "990-EZ", 2: "990-PF"}.get(code, "?")
