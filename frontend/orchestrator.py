"""
frontend/orchestrator.py
------------------------
Phase 4b — Orchestrator admin tab.

Single page (admin-only) with:
  - Header: enabled/disabled + jobs table (next fire time + cron)
  - Per-job "Run now" button (fires via /orchestrator/trigger)
  - Recent runs table with status badges and durations
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.api import APIError, GProspectAPI


# Display-friendly labels for the three jobs.
JOB_LABELS = {
    "nightly_health_check":     "🏥 Health check (daily)",
    "weekly_discovery":         "🔍 Discovery (weekly per org)",
    "biweekly_grants_ingestion":"📥 Grants ingestion (biweekly per org)",
}

STATUS_BADGE = {
    "started": "🟡 Running",
    "success": "🟢 Success",
    "failed":  "🔴 Failed",
}


def render_orchestrator(api: GProspectAPI, user: dict) -> None:
    st.title("🔄 Orchestrator")
    st.caption(
        "Scheduled discovery + ingestion + health-check jobs. "
        "Defaults: health check daily 04:00 UTC, discovery Mondays 06:00 UTC, "
        "grants ingestion on the 1st and 15th 06:00 UTC. Override via the "
        "`ORCHESTRATOR_*_CRON` env vars."
    )

    if user.get("role") != "admin":
        st.warning("Admin-only surface. Sign in as an admin to see scheduler state.")
        return

    try:
        data = api.get_orchestrator_status()
    except APIError as e:
        st.error(f"Could not load orchestrator status: {e.detail}")
        return

    # ── Status header ────────────────────────────────────────────────────────
    enabled = bool(data.get("enabled"))
    with st.container(border=True):
        c1, c2 = st.columns([1, 3])
        c1.metric("Scheduler", "🟢 Running" if enabled else "⛔ Disabled")
        c2.caption(
            "Disable with `ORCHESTRATOR_ENABLED=0` in `.env` and restart "
            "the portal. Test environments default to disabled to keep "
            "pytest fast."
        )

    # ── Jobs table ───────────────────────────────────────────────────────────
    st.subheader("Scheduled jobs")
    jobs = data.get("jobs") or []
    if not jobs:
        st.info(
            "No jobs registered. Either the scheduler is disabled, or "
            "the portal hasn't completed startup yet."
        )
    else:
        rows = []
        for j in jobs:
            rows.append({
                "Job":            JOB_LABELS.get(j["id"], j["id"]),
                "Next fire (UTC)":j.get("next_fire_time") or "—",
                "Trigger":        j.get("trigger") or "",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        st.markdown("**Run a job manually:**")
        c_cols = st.columns(3)
        for i, j in enumerate(jobs):
            with c_cols[i % 3]:
                if st.button(f"▶ {JOB_LABELS.get(j['id'], j['id'])}",
                             key=f"trigger_{j['id']}",
                             use_container_width=True):
                    try:
                        resp = api.trigger_orchestrator_job(j["id"])
                    except APIError as e:
                        st.error(f"Trigger failed: {e.detail}")
                    else:
                        st.success(resp.get("message") or "Queued.")

    # ── Recent runs ──────────────────────────────────────────────────────────
    st.subheader("Recent runs")
    runs = data.get("recent_runs") or []
    if not runs:
        st.caption("No runs recorded yet.")
        return

    rows = []
    for r in runs:
        rows.append({
            "Started":  r.get("started_at"),
            "Job":      JOB_LABELS.get(r.get("job_name"), r.get("job_name")),
            "Org ID":   r.get("org_id") if r.get("org_id") is not None else "—",
            "Status":   STATUS_BADGE.get(r.get("status"), r.get("status")),
            "Duration": _fmt_ms(r.get("duration_ms")),
            "Message":  (r.get("message") or "")[:140],
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


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
