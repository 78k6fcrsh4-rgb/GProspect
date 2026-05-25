"""
portal/main.py
--------------
FastAPI application entry point for the grant prospecting portal.

Assembles all routers, configures middleware, creates database
tables on startup, and seeds the first admin user.

Run with:
    uvicorn portal.main:app --reload --port 8000

Then open in browser:
    http://localhost:8000/docs  — interactive API documentation
    http://localhost:8000       — portal root
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from portal.limiter import limiter

load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan — runs on startup and shutdown
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Handles application startup and shutdown.

    On startup:
        - Creates all database tables
        - Seeds the first admin user if no users exist

    On shutdown:
        - Logs shutdown message
    """
    # ── Startup ───────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  GRANT PROSPECTING PORTAL — STARTING")
    print("  AI for Good — P33 Chicago")
    print("="*60)

    # Create database tables, then run idempotent schema patches for
    # additive changes that create_all() won't apply to existing tables.
    from database.db import create_tables, run_lightweight_migrations
    create_tables()
    run_lightweight_migrations()

    # Seed tenant orgs (Deborah's Place, Found Village) — must run before
    # _seed_initial_admin so the admin row can be attached to an org_id.
    # Also re-runs the lightweight migration's backfill step in case the
    # orgs table didn't yet exist when migrations first executed (fresh DBs
    # take this path).
    _seed_initial_orgs()
    run_lightweight_migrations()

    # Seed first admin user if no users exist
    _seed_initial_admin()

    print("  Portal ready at http://localhost:8000")
    print("  API docs at    http://localhost:8000/docs")
    print("="*60 + "\n")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    print("\n[Portal] Shutting down gracefully...")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "Grant Prospecting Agent Portal",
    description = (
        "AI-powered grant prospecting portal for nonprofit organizations. "
        "Built by AI for Good — P33 Chicago."
    ),
    version     = "1.0.0",
    lifespan    = lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting (slowapi)
# Attach the limiter to app.state so decorated routes can find it via the
# request, and register the standard 429 handler.
# ─────────────────────────────────────────────────────────────────────────────

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ─────────────────────────────────────────────────────────────────────────────
# CORS middleware
# Origins come from the environment. Two knobs:
#
#   CORS_ALLOWED_ORIGINS   Comma-separated whitelist, e.g.
#                          "https://portal.example.org,https://app.example.org"
#                          Sets allow_credentials=True (cookies, Auth header).
#
#   CORS_ALLOW_ALL=1       Local-dev escape hatch. Sets origins=["*"] AND forces
#                          allow_credentials=False — the wildcard-with-credentials
#                          combo is rejected by every modern browser and is the
#                          most common CORS misconfiguration in the wild.
#
# Unset both → no cross-origin requests are accepted. Same-origin still works.
# ─────────────────────────────────────────────────────────────────────────────

_cors_env       = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
_cors_allow_all = os.getenv("CORS_ALLOW_ALL", "").strip().lower() in {"1", "true", "yes"}

if _cors_allow_all:
    _cors_origins     = ["*"]
    _cors_credentials = False
    print(
        "[security] WARNING: CORS_ALLOW_ALL is enabled — origins='*', credentials disabled. "
        "Use only for local development.",
    )
elif _cors_env:
    _cors_origins     = [o.strip() for o in _cors_env.split(",") if o.strip()]
    _cors_credentials = True
else:
    _cors_origins     = []
    _cors_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins     = _cors_origins,
    allow_credentials = _cors_credentials,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Routers
# Each router handles a specific area of functionality.
# ─────────────────────────────────────────────────────────────────────────────

from portal.routers.auth     import router as auth_router
from portal.routers.results  import router as results_router
from portal.routers.admin    import router as admin_router
from portal.routers.feedback import router as feedback_router
from portal.routers.orgs     import router as orgs_router
from portal.routers.profiles import router as profiles_router

app.include_router(auth_router)
app.include_router(results_router)
app.include_router(admin_router)
app.include_router(feedback_router)
app.include_router(orgs_router)
app.include_router(profiles_router)


# ─────────────────────────────────────────────────────────────────────────────
# Root endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    """Portal root — returns basic info about the running application."""
    return {
        "name":        "Grant Prospecting Agent Portal",
        "version":     "1.0.0",
        "status":      "running",
        "docs":        "/docs",
        "description": "AI-powered grant prospecting for nonprofits — AI for Good",
    }


@app.get("/health")
def health_check():
    """
    Health check endpoint.
    Used to verify the portal is running correctly.
    Returns 200 OK if everything is working.
    """
    from database.db import get_db_stats
    try:
        db_stats = get_db_stats()
        return {
            "status":   "healthy",
            "database": "connected",
            "tables":   db_stats.get("tables", {}),
        }
    except Exception as e:
        return JSONResponse(
            status_code = 503,
            content     = {"status": "unhealthy", "error": str(e)},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Startup helper
# ─────────────────────────────────────────────────────────────────────────────

def _seed_initial_orgs() -> None:
    """
    Idempotently seeds the two pilot organizations: Deborah's Place (Chicago,
    women's housing) and Found Village (Cincinnati, youth welfare).

    Also imports the existing profiles/deborah_place.json into the v2
    org_profile_versions table as version 1 if no current profile exists
    for Deborah's Place yet. Found Village starts without a profile version
    — Phase 1 intake creates the first version.

    Safe to call on every startup. No-op if both orgs already exist and
    Deborah's Place already has a current profile version.
    """
    import json
    from pathlib import Path

    from database.db import SessionLocal
    from portal.models.organization import Organization, OrgStatus
    from portal.models.org_profile  import OrgProfileVersion, create_next_version

    PILOTS = [
        # slug, display_name
        ("deborahs-place", "Deborah's Place"),
        ("found-village",  "Found Village"),
    ]

    # Profile-import map: which pilots have a JSON profile to seed as v1.
    PROFILE_IMPORTS = {
        "deborahs-place": Path("profiles") / "deborah_place.json",
    }

    db = SessionLocal()
    try:
        # Step 1 — ensure both orgs exist.
        for slug, display_name in PILOTS:
            existing = db.query(Organization).filter_by(slug=slug).one_or_none()
            if existing is None:
                org = Organization(
                    slug         = slug,
                    display_name = display_name,
                    status       = OrgStatus.ACTIVE,
                    settings     = {},
                )
                db.add(org)
                print(f"[Portal] Seeded org: {slug} ({display_name!r})")
        db.commit()

        # Step 2 — import the v1 JSON profile for any pilot that doesn't yet
        # have a current OrgProfileVersion.
        for slug, profile_path in PROFILE_IMPORTS.items():
            org = db.query(Organization).filter_by(slug=slug).one_or_none()
            if org is None:
                # Shouldn't happen because we just seeded — defensive.
                continue

            has_current = (
                db.query(OrgProfileVersion)
                  .filter(OrgProfileVersion.org_id == org.id,
                          OrgProfileVersion.is_current == True)  # noqa: E712
                  .count()
            ) > 0
            if has_current:
                continue

            if not profile_path.exists():
                print(
                    f"[Portal] Profile file missing for {slug}: {profile_path} "
                    f"— skipping initial version import."
                )
                continue

            try:
                with profile_path.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                create_next_version(
                    db,
                    org_id             = org.id,
                    payload            = payload,
                    created_by_user_id = None,   # system-seeded
                )
                db.commit()
                print(f"[Portal] Imported initial OrgProfileVersion for {slug}")
            except Exception as e:
                db.rollback()
                print(f"[Portal] Failed to import profile for {slug}: {e}")

    except Exception as e:
        print(f"[Portal] Error seeding orgs: {e}")
        db.rollback()
    finally:
        db.close()


def _seed_initial_admin() -> None:
    """
    Creates the first Admin user for Deborah's Place if no users exist.

    Credentials are read from environment variables.
    Set these in your .env file before starting the portal:

        ADMIN_EMAIL    = admin@deborahsplace.org
        ADMIN_PASSWORD = your_secure_password_here
        ADMIN_NAME     = Mary Kelly
        ADMIN_ORG      = Deborah's Place

    If these are not set, default credentials are used.
    Change the password immediately after first login.

    As of v2 Phase 0 the admin row is tied to the Deborah's Place
    Organization via org_id; org_name remains as a denormalized convenience.
    """
    from database.db import SessionLocal
    from portal.models.user         import User, UserRole
    from portal.models.organization import Organization
    from portal.auth.security       import hash_password

    db = SessionLocal()
    try:
        existing_users = db.query(User).count()
        if existing_users > 0:
            print(f"[Portal] {existing_users} user(s) already exist — skipping seed")
            return

        admin_email    = os.getenv("ADMIN_EMAIL",    "admin@deborahsplace.org")
        admin_password = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
        admin_name     = os.getenv("ADMIN_NAME",     "Mary Kelly")
        admin_org      = os.getenv("ADMIN_ORG",      "Deborah's Place")

        # Find the matching Organization row (created by _seed_initial_orgs).
        # We match on display_name first; fall back to slug for forgiving env
        # values like "deborahs-place".
        org_row = (
            db.query(Organization)
              .filter(
                  (Organization.display_name == admin_org) |
                  (Organization.slug         == admin_org)
              )
              .one_or_none()
        )
        if org_row is None:
            raise RuntimeError(
                f"Cannot seed admin user — no Organization row matches "
                f"ADMIN_ORG={admin_org!r}. Did _seed_initial_orgs() run?"
            )

        admin = User(
            email           = admin_email,
            full_name       = admin_name,
            org_id          = org_row.id,
            org_name        = org_row.display_name,
            hashed_password = hash_password(admin_password),
            role            = UserRole.ADMIN,
            is_active       = True,
            is_verified     = True,
        )

        db.add(admin)
        db.commit()

        print(f"[Portal] First admin created: {admin_email}")
        print(f"[Portal] Organization: {org_row.display_name} (slug={org_row.slug})")
        print(f"[Portal] ⚠️  Change the default password immediately after login")

    except Exception as e:
        print(f"[Portal] Error seeding admin: {e}")
        db.rollback()
    finally:
        db.close()