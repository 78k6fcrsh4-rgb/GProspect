"""
frontend/capacity.py
--------------------
Phase 4a — Capacity sidebar entry.

Single page with:
  - Header summary (current pursuing / target / utilization)
  - Editable target number
  - Editable availability windows (st.data_editor)
  - Save button → PUT /orgs/me/capacity
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.api import APIError, GProspectAPI


def render_capacity(api: GProspectAPI, user: dict) -> None:
    st.title("⚖️ Capacity")
    st.caption(
        "How much pursuit work can your team realistically take on? "
        "The agent uses this to demote opportunities whose deadlines "
        "fall inside a no-availability window, and to warn when you're "
        "at or over your active-pursuits target."
    )

    # ── Current state ────────────────────────────────────────────────────────
    try:
        summary = api.get_capacity_summary()
    except APIError as e:
        st.error(f"Could not load capacity summary: {e.detail}")
        return

    cols = st.columns(4)
    cols[0].metric("Target", summary.get("active_pursuits_target", "—"))
    cols[1].metric("Currently pursuing", summary.get("current_pursuing", 0))
    headroom = summary.get("headroom")
    cols[2].metric(
        "Headroom",
        headroom if headroom is not None else "—",
        delta_color = "off",
    )
    util = summary.get("utilization_pct") or 0
    cols[3].metric("Utilization", f"{util:.0f}%")

    active_windows = summary.get("closed_windows_active") or []
    next_window    = summary.get("next_closed_window")
    if active_windows:
        labels = ", ".join(w.get("label") or "Closed window" for w in active_windows)
        st.warning(
            f"⏸️ You're currently inside a closed window: **{labels}**. "
            f"Opportunities with deadlines today will be demoted."
        )
    if next_window and not active_windows:
        st.info(
            f"Next closed window: **{next_window.get('label') or 'Closed window'}** "
            f"({next_window.get('start')} → {next_window.get('end')})."
        )

    st.divider()

    # ── Editor ───────────────────────────────────────────────────────────────
    try:
        current = api.get_capacity()
    except APIError as e:
        st.error(f"Could not load capacity row: {e.detail}")
        return

    if user.get("role") != "admin":
        st.info("Only admins can change capacity. Showing read-only view.")
        _render_readonly(current)
        return

    st.subheader("Edit capacity")
    target = st.number_input(
        "Active pursuits target",
        min_value = 1, max_value = 100, step = 1,
        value     = int(current.get("active_pursuits_target") or 5),
        help      = "How many concurrent proposals your team can run. "
                    "We warn when you're at this number, demote new "
                    "prospects when you're over.",
    )

    st.markdown("**Availability windows**")
    st.caption(
        "Periods where the team can't reasonably commit to new pursuits "
        "(holiday freezes, board transitions, busy program seasons). "
        "Opportunities with deadlines inside a window are demoted and "
        "flagged on the prospect card."
    )

    windows = current.get("availability_windows") or []
    rows_for_edit = [
        {
            "Label":  w.get("label") or "Closed window",
            "Start":  w.get("start"),
            "End":    w.get("end"),
        }
        for w in windows
    ]
    edited = st.data_editor(
        rows_for_edit,
        num_rows = "dynamic",
        column_config = {
            "Label": st.column_config.TextColumn("Label"),
            "Start": st.column_config.TextColumn("Start (YYYY-MM-DD)"),
            "End":   st.column_config.TextColumn("End (YYYY-MM-DD)"),
        },
        use_container_width = True,
        key                 = "capacity_windows_editor",
    )

    if st.button("💾 Save capacity", type="primary"):
        cleaned = []
        for row in edited or []:
            if isinstance(row, dict):
                start = (row.get("Start") or "").strip()
                end   = (row.get("End")   or "").strip()
                if start and end:
                    cleaned.append({
                        "start": start,
                        "end":   end,
                        "label": (row.get("Label") or "").strip() or "Closed window",
                    })
        try:
            api.put_capacity(
                active_pursuits_target = int(target),
                availability_windows   = cleaned,
            )
        except APIError as e:
            st.error(f"Save failed: {e.detail}")
            return
        st.success("Capacity saved. Refresh Prospects to see updated rankings.")


def _render_readonly(current: dict) -> None:
    st.metric("Active pursuits target", current.get("active_pursuits_target", "—"))
    windows = current.get("availability_windows") or []
    if not windows:
        st.caption("_(No availability windows configured.)_")
        return
    df = pd.DataFrame([
        {
            "Label": w.get("label") or "Closed window",
            "Start": w.get("start"),
            "End":   w.get("end"),
        }
        for w in windows
    ])
    st.dataframe(df, hide_index=True, use_container_width=True)
