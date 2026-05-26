"""
frontend/sources.py
-------------------
Phase 5 — 📡 Sources admin tab.

Lists every MonitoredSource visible to the caller (own org + global)
with health badges (green/yellow/red/unknown), a per-source expander
showing last-error + recent runs, and admin-only controls to add,
edit, enable/disable, delete, and manually check sources.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.api import APIError, GProspectAPI


HEALTH_BADGE = {
    "green":   "🟢 Healthy",
    "yellow":  "🟡 Stale / had failures",
    "red":     "🔴 Failing",
    "unknown": "⚪ Never checked",
}

KIND_DESCRIPTIONS = {
    "rss":    "RSS / Atom feed — parsed for new entries",
    "page":   "Generic HTML — content-hash diffing only",
    "custom": "Custom parser — extracts structured items",
}


def render_sources(api: GProspectAPI, user: dict) -> None:
    st.title("📡 Sources")
    st.caption(
        "Monitored URLs we check periodically for new grant content. Daily "
        "orchestrator job runs at 03:00 UTC; you can also fire a check "
        "manually below."
    )

    is_admin = user.get("role") == "admin"

    try:
        sources = api.list_sources()
    except APIError as e:
        st.error(f"Could not load sources: {e.detail}")
        return

    if not sources:
        st.info("No sources configured yet.")
    else:
        _render_summary_table(sources)
        st.write("")
        for src in sources:
            _render_source_row(api, src, is_admin)

    # ── Admin: add new source ────────────────────────────────────────────────
    if is_admin:
        st.divider()
        with st.expander("➕ Add a new source", expanded=False):
            _render_add_form(api)


def _render_summary_table(sources: list[dict]) -> None:
    rows = []
    for s in sources:
        rows.append({
            "Health":  HEALTH_BADGE.get(s.get("health"), s.get("health") or "—"),
            "Name":    s["name"],
            "Kind":    s["kind"],
            "Enabled": "✓" if s.get("enabled") else "✗",
            "Last success": (s.get("last_success_at") or "—").split("T")[0]
                            if s.get("last_success_at") else "—",
            "Failures (consecutive)": s.get("failure_count") or 0,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def _render_source_row(api: GProspectAPI, src: dict, is_admin: bool) -> None:
    health_badge = HEALTH_BADGE.get(src.get("health"), "")
    scope_label  = "global" if src.get("org_id") is None else "your org"

    label = (
        f"{health_badge}  ·  **{src['name']}**  ·  "
        f"{src['kind']} · {scope_label}"
    ).strip(" ·")

    with st.expander(label, expanded=False):
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown(f"**URL:** `{src['url']}`")
            if src.get("parser_key"):
                st.markdown(f"**Parser:** `{src['parser_key']}`")
            st.caption(KIND_DESCRIPTIONS.get(src.get("kind"), ""))
            if src.get("last_error"):
                st.error(f"Last error: {src['last_error']}")
            if src.get("last_success_at"):
                st.caption(f"✓ Last success: {src['last_success_at']}")
            if src.get("last_failure_at"):
                st.caption(f"✗ Last failure: {src['last_failure_at']}")

        with c2:
            if is_admin:
                if st.button("▶ Check now", key=f"check_{src['id']}",
                              type="primary", use_container_width=True):
                    try:
                        api.trigger_source_check(src["id"])
                    except APIError as e:
                        st.error(f"Trigger failed: {e.detail}")
                    else:
                        st.success("Check queued.")
                new_enabled = st.toggle(
                    "Enabled",
                    value = bool(src.get("enabled")),
                    key   = f"enabled_{src['id']}",
                )
                if new_enabled != bool(src.get("enabled")):
                    try:
                        api.update_source(src["id"], {"enabled": new_enabled})
                    except APIError as e:
                        st.error(f"Update failed: {e.detail}")
                    else:
                        st.rerun()
                if st.button("🗑 Delete", key=f"delete_{src['id']}",
                              use_container_width=True):
                    try:
                        api.delete_source(src["id"])
                    except APIError as e:
                        st.error(f"Delete failed: {e.detail}")
                    else:
                        st.rerun()

        # Recent runs
        st.markdown("**Recent runs**")
        try:
            runs = api.get_source_runs(src["id"])
        except APIError as e:
            st.caption(f"Couldn't load runs: {e.detail}")
            return
        if not runs:
            st.caption("_No checks recorded yet._")
            return
        run_rows = []
        for r in runs[:10]:
            run_rows.append({
                "Started":      r.get("started_at"),
                "Status":       r.get("status"),
                "Items":        r.get("items_found"),
                "Duration":     _fmt_ms(r.get("duration_ms")),
                "Message":      (r.get("message") or "")[:100],
            })
        st.dataframe(pd.DataFrame(run_rows),
                     hide_index=True, use_container_width=True)


def _render_add_form(api: GProspectAPI) -> None:
    with st.form("add_source_form", clear_on_submit=True):
        name = st.text_input("Display name")
        url  = st.text_input("URL", placeholder="https://example.org/grants/")
        col1, col2 = st.columns(2)
        kind = col1.selectbox("Kind", ["page", "rss", "custom"])
        scope = col2.selectbox("Scope", ["org", "global"])
        parser_key = ""
        if kind == "custom":
            parser_key = st.text_input(
                "Parser key",
                placeholder = "e.g. gcf / macarthur / interact_for_health",
            )
        submitted = st.form_submit_button("Add source", type="primary")

    if submitted:
        if not name or not url:
            st.warning("Name and URL are both required.")
            return
        try:
            api.create_source({
                "name":       name,
                "url":        url,
                "kind":       kind,
                "parser_key": parser_key or None,
                "scope":      scope,
                "config":     {},
                "enabled":    True,
            })
        except APIError as e:
            st.error(f"Could not create source: {e.detail}")
            return
        st.success(f"Added {name!r}. The next daily run (03:00 UTC) will check it.")
        st.rerun()


def _fmt_ms(ms) -> str:
    if ms is None:
        return "—"
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return str(ms)
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000.0:.1f} s"
