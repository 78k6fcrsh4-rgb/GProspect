# GProspect — Grant Prospecting AI Agent

Free, automated grant prospecting for US nonprofits. Ingests open grant sources, scores opportunities against a structured organization profile, and serves results through a FastAPI portal with role-based auth and an autonomous learning loop. Built for AI for Good — P33 Chicago. Pilot organization: Deborah's Place (Chicago homelessness services).

## What it does

You give it a JSON profile of your nonprofit — mission, programs, geography, populations served, grant size range, funder exclusions — and it produces a ranked list of currently open grant opportunities, with hot / warm / cold deadline classification and AI-generated match explanations. Results are persisted to SQLite (or Postgres) and exposed through a web portal that supports login, results browsing, admin operations, and a feedback-driven learning loop that improves matching over time.

Two entry points share the same agent core:
- **CLI**: `run_agent.py` runs the full pipeline once and exports CSV / Excel.
- **Web portal**: `portal/main.py` (FastAPI) serves the same results plus auth, admin, and the learning loop.

## Quick start (local)

You need Python 3.10+, an Anthropic API key, and a Mac or Linux shell.

```bash
cd ~/WorkBench/AI4GSH/lsrmba777

# One-shot setup + boot (creates .venv, installs deps, copies .env, runs uvicorn):
bash ~/WorkBench/AI4GSH/run_portal.sh
```

That script lives at the parent level so it can target any working copy. To do it by hand instead:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env           # then edit .env, set ANTHROPIC_API_KEY
uvicorn portal.main:app --reload --port 8000
```

Then open `http://localhost:8000/docs` for the interactive API, or `http://localhost:8000` for the portal root.

The first admin user is seeded on startup from `ADMIN_EMAIL` / `ADMIN_PASSWORD` in your `.env`. Default if unset: `admin@deborahsplace.org` / `ChangeMe123!` — rotate it immediately.

## CLI usage

```bash
# Full pipeline (default 10 search queries)
python3 run_agent.py --profile profiles/deborah_place.json

# Deeper search
python3 run_agent.py --profile profiles/deborah_place.json --queries 20

# Targeted program area
python3 run_agent.py --profile profiles/deborah_place.json --program workforce_development

# Single custom query
python3 run_agent.py --profile profiles/deborah_place.json --search "MacArthur Foundation open grants 2026"

# Skip AI scoring (faster, no API cost)
python3 run_agent.py --profile profiles/deborah_place.json --no-scoring
```

Outputs land in `outputs/` as CSV, Excel (if `openpyxl` available), and a run summary.

## Environment variables

Copy `.env.example` (in the parent `AI4GSH/` folder) to `lsrmba777/.env` and fill in:

| Variable | Required? | Purpose |
|----------|-----------|---------|
| `ANTHROPIC_API_KEY` | Yes for AI features | Web search + scoring + learning-loop gap analysis |
| `SECRET_KEY` | Yes for any non-local use | JWT signing. See **Security notes** below. |
| `ADMIN_EMAIL` | No | First-run admin user email (default: `admin@deborahsplace.org`) |
| `ADMIN_PASSWORD` | No | First-run admin password (default: `ChangeMe123!`) |
| `ADMIN_NAME` | No | First-run admin display name |
| `ADMIN_ORG` | No | First-run admin organization name |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | JWT lifetime (default 480 = 8 hours) |
| `DATABASE_URL` | No | Postgres URL. Empty = local SQLite at `./grant_prospector.db` |

## Project structure

```
agent/        Core agent loop, profile parsing, scheduler, prompt builder
portal/       FastAPI web app — main.py, auth/, models/, routers/
database/     SQLAlchemy engine + session factory + table creation
tools/        Data source integrations: web_search, grants.gov, IRS 990
scoring/      Anthropic-backed match scorer
learning/     Feedback processor + gap analyzer (autonomous improvement loop)
profiles/     Organization profile JSONs (deborah_place.json, template)
prompts/      LLM prompt templates
cycles/       Persisted run state for the autonomous scheduler
output/       CSV / Excel / summary exporters
outputs/      Where run_agent.py writes its result files and the
              learning loop persists state, submissions, and history
```

## Pilot organizations

`profiles/deborah_place.json` ships pre-configured for Deborah's Place. To onboard another nonprofit, copy `profiles/org_profile_template.json` and fill in the fields.

## Security notes

- `SECRET_KEY` controls JWT signing. If unset (or left at the placeholder string), the portal will generate an **ephemeral** random key at startup and warn loudly. JWTs will be invalidated on every restart and the deployment is not safe. Always set `SECRET_KEY` to a long random string before exposing the portal anywhere.
- The seeded admin password (`ChangeMe123!`) is intentionally embarrassing. Change it on first login.
- CORS is currently `allow_origins=["*"]` in `portal/main.py`. Restrict before going to production.

## Recent fixes (2026-05-23)

This commit cleans up several issues discovered during a local-run audit. None of them changed behavior of the running code; they just made the project install correctly, fail safely on misconfiguration, and stop checking in transient files.

**1. README and `requirements.txt` rewritten.** The previous versions described a Streamlit app architecture (`app/main.py`, `streamlit`/`feedparser`/`sendgrid` dependencies) that did not match the actual FastAPI codebase in this repo. `pip install -r requirements.txt` was installing the wrong stack and missing everything the portal actually needed (FastAPI, SQLAlchemy, python-jose, passlib, anthropic, openpyxl). The new files describe what's really here.

**2. `SECRET_KEY` fallback hardened.** `portal/auth/security.py` previously fell back silently to a known placeholder string when `SECRET_KEY` was unset, meaning a forgotten environment variable would produce a deployment with a publicly-knowable JWT signing key. It now generates a one-shot random key at startup and prints a clear warning, so misconfiguration is loud rather than silent.

**3. SQLite WAL sidecars untracked.** `grant_prospector.db-shm` and `grant_prospector.db-wal` were committed to git. The previous `.gitignore` matched `*.db` but did not cover SQLite's `-shm` / `-wal` companion files, which churn on every read/write. They are now removed from the repo and `.gitignore` covers them.

**4. Editor backup untracked.** `portal/models/learning.py~` (an editor swap/backup) was committed. Removed from the repo; `.gitignore` now covers `*~` and `*.swp`.

## License

MIT — use and adapt freely for nonprofit grant prospecting.
