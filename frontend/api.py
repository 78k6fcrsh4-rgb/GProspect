"""
frontend/api.py
---------------
Tiny HTTP client wrapping the GProspect FastAPI portal. All Streamlit
code goes through this — no `requests.get(...)` calls scattered across
pages — so error handling, timeouts, and the JWT header live in one
place.

Pattern:

    from frontend.api import GProspectAPI, APIError

    api = GProspectAPI(token=st.session_state.token)
    profile = api.get_current_profile()
    api.save_profile_version(payload)

Errors are raised as APIError with .status_code and .detail so the UI
can decide how to surface them (toast, inline form error, redirect to
login on 401, etc).
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

API_URL = os.getenv("PORTAL_API_URL", "http://localhost:8000").rstrip("/")
REQUEST_TIMEOUT_SECONDS = 60   # bumped from 30 — doc-assist hits Claude


class APIError(Exception):
    """Wraps a non-2xx response from the portal."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail      = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class GProspectAPI:
    """Thin client. One instance per render — cheap to construct."""

    def __init__(self, token: Optional[str] = None, base_url: str = API_URL):
        self.token    = token
        self.base_url = base_url.rstrip("/")

    # ── Internal HTTP helpers ────────────────────────────────────────────────

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h: dict[str, str] = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def _raise(self, resp: requests.Response) -> None:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text or "(no body)"
            raise APIError(resp.status_code, str(detail))

    def _get(self, path: str, **kwargs) -> Any:
        resp = requests.get(
            f"{self.base_url}{path}",
            headers = self._headers(),
            timeout = REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )
        self._raise(resp)
        return resp.json()

    def _post(self, path: str, **kwargs) -> Any:
        resp = requests.post(
            f"{self.base_url}{path}",
            headers = self._headers(kwargs.pop("headers", None)),
            timeout = REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )
        self._raise(resp)
        return resp.json()

    # ── Auth ─────────────────────────────────────────────────────────────────

    def login(self, email: str, password: str) -> dict:
        resp = requests.post(
            f"{self.base_url}/auth/login",
            data    = {"username": email, "password": password},
            timeout = REQUEST_TIMEOUT_SECONDS,
        )
        self._raise(resp)
        return resp.json()

    def logout(self) -> None:
        """Best-effort — never raise."""
        try:
            requests.post(
                f"{self.base_url}/auth/logout",
                headers = self._headers(),
                timeout = REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            pass

    def me(self) -> dict:
        return self._get("/auth/me")

    # ── Org ──────────────────────────────────────────────────────────────────

    def get_my_org(self) -> dict:
        return self._get("/orgs/me")

    # ── Profile ──────────────────────────────────────────────────────────────

    def get_current_profile(self) -> Optional[dict]:
        """Returns the active profile version, or None if none exists yet."""
        try:
            return self._get("/orgs/me/profile/current")
        except APIError as e:
            if e.status_code == 404:
                return None
            raise

    def get_profile_history(self) -> list[dict]:
        return self._get("/orgs/me/profile/history")

    def save_profile_version(self, profile_payload: dict) -> dict:
        return self._post(
            "/orgs/me/profile/version",
            json = {"profile": profile_payload},
        )

    def extract_profile_from_doc(
        self,
        file_bytes: bytes,
        filename:   str,
        mime_type:  str = "application/octet-stream",
    ) -> dict:
        """
        Upload a doc to /orgs/me/profile/extract and get prefill fields back.

        Returns a dict with keys 'extracted_fields' (dict) and 'notes' (list).
        """
        resp = requests.post(
            f"{self.base_url}/orgs/me/profile/extract",
            headers = self._headers(),
            files   = {"file": (filename, file_bytes, mime_type)},
            timeout = REQUEST_TIMEOUT_SECONDS,
        )
        self._raise(resp)
        return resp.json()

    # ── Results (v1 — kept for the legacy summary) ───────────────────────────

    def get_results_summary(self) -> dict:
        return self._get("/results/summary")

    def get_results(self, *, limit: int = 50, min_score: Optional[float] = None) -> list:
        params: dict = {"limit": limit}
        if min_score is not None:
            params["min_score"] = min_score
        return self._get("/results/", params=params)

    # ── Opportunities (Phase 1b — enriched list + pursuit + narrative) ───────

    def list_opportunities(
        self,
        *,
        limit:     int = 200,
        min_score: Optional[float] = None,
        pursuit:   Optional[str]   = None,
    ) -> list[dict]:
        params: dict = {"limit": limit}
        if min_score is not None:
            params["min_score"] = min_score
        if pursuit:
            params["pursuit"] = pursuit
        return self._get("/opportunities/", params=params)

    def set_pursuit(self, opp_key: str, action: str,
                    notes: Optional[str] = None) -> dict:
        """action ∈ {'pursue', 'watch', 'pass', 'clear'}"""
        if action == "clear":
            return self._post(f"/opportunities/{opp_key}/clear")
        return self._post(
            f"/opportunities/{opp_key}/{action}",
            json = {"notes": notes} if notes else {},
        )

    def get_or_generate_narrative(self, opp_key: str) -> dict:
        return self._post(f"/opportunities/{opp_key}/narrative")

    # ── Digest ───────────────────────────────────────────────────────────────

    def generate_digest_zip(self) -> bytes:
        """
        Hit POST /digests/generate and return the raw ZIP bytes.
        Caller is responsible for offering it as a download.
        """
        resp = requests.post(
            f"{self.base_url}/digests/generate",
            headers = self._headers(),
            timeout = REQUEST_TIMEOUT_SECONDS,
        )
        self._raise(resp)
        return resp.content

    # ── Funder discovery (Phase 2) ───────────────────────────────────────────

    def list_funder_candidates(self, *,
                               status_filter: Optional[str] = None,
                               limit:         int           = 200) -> list[dict]:
        params: dict = {"limit": limit}
        if status_filter:
            params["pursuit_status"] = status_filter
        return self._get("/funders/candidates", params=params)

    def get_funder_detail(self, ein: str) -> dict:
        return self._get(f"/funders/{ein}")

    def set_candidate_status(self, ein: str, status: str,
                             notes: Optional[str] = None) -> dict:
        return self._post(
            f"/funders/{ein}/status",
            json = {"status": status, "notes": notes},
        )

    def trigger_discovery_run(self) -> dict:
        return self._post("/discovery/run")

    def get_warm_path_summary(self) -> list[dict]:
        """Per-candidate warm-path counts. Powers the Funders-list badge."""
        return self._get("/funders/warm-paths/summary")

    def get_warm_paths_for_funder(self, ein: str) -> dict:
        """Peer grants this funder has given to peers of the caller's org."""
        return self._get(f"/funders/{ein}/warm-paths")

    # ── Grants (Phase 3a) ────────────────────────────────────────────────────

    def trigger_grants_ingest(self) -> dict:
        return self._post("/grants/ingest")

    def get_grants_status(self) -> list[dict]:
        return self._get("/grants/status")

    # ── Capacity (Phase 4a) ──────────────────────────────────────────────────

    def get_capacity(self) -> dict:
        return self._get("/orgs/me/capacity")

    def put_capacity(self, *,
                     active_pursuits_target: int,
                     availability_windows: list[dict]) -> dict:
        resp = requests.put(
            f"{self.base_url}/orgs/me/capacity",
            headers = self._headers(),
            json    = {
                "active_pursuits_target": active_pursuits_target,
                "availability_windows":   availability_windows,
            },
            timeout = REQUEST_TIMEOUT_SECONDS,
        )
        self._raise(resp)
        return resp.json()

    def get_capacity_summary(self) -> dict:
        return self._get("/opportunities/capacity-summary")

    # ── Orchestrator (Phase 4b) ──────────────────────────────────────────────

    def get_orchestrator_status(self) -> dict:
        return self._get("/orchestrator/status")

    def trigger_orchestrator_job(self, job_name: str) -> dict:
        return self._post(f"/orchestrator/trigger/{job_name}")

    # ── Sources (Phase 5) ────────────────────────────────────────────────────

    def list_sources(self) -> list[dict]:
        return self._get("/sources/")

    def create_source(self, payload: dict) -> dict:
        return self._post("/sources/", json=payload)

    def update_source(self, source_id: int, payload: dict) -> dict:
        resp = requests.put(
            f"{self.base_url}/sources/{source_id}",
            headers = self._headers(),
            json    = payload,
            timeout = REQUEST_TIMEOUT_SECONDS,
        )
        self._raise(resp)
        return resp.json()

    def delete_source(self, source_id: int) -> None:
        resp = requests.delete(
            f"{self.base_url}/sources/{source_id}",
            headers = self._headers(),
            timeout = REQUEST_TIMEOUT_SECONDS,
        )
        self._raise(resp)

    def trigger_source_check(self, source_id: int) -> dict:
        return self._post(f"/sources/{source_id}/check")

    def get_source_runs(self, source_id: int) -> list[dict]:
        return self._get(f"/sources/{source_id}/runs")
