"""
frontend/account.py
-------------------
👤 Account tab — self-service password change.

The only thing here for now is a password-change form. Future surface
(email change, profile photo, theme preference, etc.) can grow from
this module without touching the rest of the app.

Backend contract:
  POST /auth/change-password { current_password, new_password }
    - 200 on success
    - 400 if current password wrong or new password < 8 chars
    - 429 if rate limit exceeded (10/hour/IP)
"""

from __future__ import annotations

import streamlit as st

from frontend.api import APIError, GProspectAPI


MIN_PASSWORD_LEN = 8


def render_account(api: GProspectAPI, user: dict) -> None:
    st.title("👤 Account")
    st.caption(
        f"Signed in as **{user.get('full_name') or user.get('email')}** "
        f"({user.get('email')}) — {user.get('role')} of "
        f"{user.get('org_name')}."
    )

    st.divider()

    # ── Change password ──────────────────────────────────────────────────────
    st.subheader("Change password")
    st.caption(
        f"Pick something at least {MIN_PASSWORD_LEN} characters. Rate-limited "
        f"at 10 attempts per hour per source IP."
    )

    with st.form("change_password", clear_on_submit=True):
        current = st.text_input("Current password", type="password",
                                key="cp_current")
        new1    = st.text_input("New password",     type="password",
                                key="cp_new1")
        new2    = st.text_input("Confirm new password", type="password",
                                key="cp_new2")
        submitted = st.form_submit_button("Update password",
                                           type               = "primary",
                                           use_container_width= True)

    if not submitted:
        return

    # ── Client-side validation (cheap; mirrors the server's rules) ───────────
    if not current or not new1 or not new2:
        st.warning("All three fields are required.")
        return
    if new1 != new2:
        st.error("New password and confirmation do not match.")
        return
    if len(new1) < MIN_PASSWORD_LEN:
        st.error(f"New password must be at least {MIN_PASSWORD_LEN} characters.")
        return
    if new1 == current:
        st.warning("New password is the same as the current one.")
        return

    # ── Submit to backend ────────────────────────────────────────────────────
    try:
        api.change_password(
            current_password = current,
            new_password     = new1,
        )
    except APIError as e:
        if e.status_code == 400:
            st.error(f"Couldn't change password: {e.detail}")
        elif e.status_code == 429:
            st.error("Too many attempts. Wait up to an hour and try again.")
        else:
            st.error(f"Couldn't change password: {e.detail}")
        return

    st.success(
        "✅ Password updated. The next time you sign out (or your current "
        "session expires) you'll need the new password."
    )
