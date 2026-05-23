"""
portal/limiter.py
-----------------
Shared slowapi Limiter instance for IP-based rate limiting.

Initialized once at module import. Imported by:
    - portal/main.py — to wire the RateLimitExceeded exception handler
                       and attach the limiter to app.state.
    - portal/routers/auth.py — to decorate login / change-password.

Defaults to in-memory storage. For multi-worker / multi-host deployments,
set the RATE_LIMIT_STORAGE_URI environment variable to a Redis URL
(e.g. "redis://localhost:6379/0") so the limit state is shared across
processes. Without that, each worker has its own counter and the limit
is effectively N × per-worker.

Rate limit syntax follows the "X per period" format that Limits parses:
    "5/minute", "10/hour", "100/day"
"""

from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

_storage_uri = os.getenv("RATE_LIMIT_STORAGE_URI", "memory://").strip() or "memory://"

limiter = Limiter(
    key_func     = get_remote_address,
    storage_uri  = _storage_uri,
    # Default rate applied to any decorated route that doesn't override.
    default_limits = ["1000/hour"],
)
