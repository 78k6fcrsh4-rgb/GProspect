# Grant Prospecting Tool

Free, automated grant prospecting for US nonprofits. Ingests public grant listings, scores them against your organization profile, and surfaces **Act Now**, **Coming Up**, and **Prospect / Historical** leads—no paid subscriptions required for end users.

## Features

- Organization profile (mission, programs, geography, grant size range)
- Free data sources: Philanthropy News Digest RSS, Grants.gov, ProPublica 990, Grantmakers.io
- Rule-based matching and deadline classification (hot / warm / less warm)
- Streamlit dashboard, manual entry, optional email alerts (SendGrid free tier)
- Daily automation via GitHub Actions
- SQLite locally; Postgres (Supabase) for hosted deploy

## Quick start (local)

```bash
cd grant-prospecting-tool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Seed pilot orgs and run agent once
python scripts/seed_pilot_orgs.py

# Launch app
streamlit run app/main.py
```

Open http://localhost:8501 → **Organization Profile** → **Run Agent** → **Dashboard**.

Default admin (after seed): `admin@example.org` / `changeme123`

## Environment variables

See [.env.example](.env.example). For local dev, no keys are required.

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Postgres connection string (Supabase). Empty = SQLite in `data/` |
| `SENDGRID_API_KEY` | Hot-lead email alerts |
| `SENDGRID_FROM_EMAIL` | Verified sender in SendGrid |
| `APP_SECRET_KEY` | Session signing |
| `ANTHROPIC_API_KEY` | Optional BYOK match explanations |

## Deploy on Streamlit Community Cloud

1. Push this repo to GitHub (public repo for free tier).
2. [share.streamlit.io](https://share.streamlit.io) → New app → select repo.
3. Main file path: `app/main.py`
4. Add secrets (Settings → Secrets):

```toml
DATABASE_URL = "postgresql://..."
APP_SECRET_KEY = "long-random-string"
SENDGRID_API_KEY = "..."
SENDGRID_FROM_EMAIL = "alerts@yourdomain.com"
```

5. Run [supabase/schema.sql](supabase/schema.sql) in Supabase SQL editor.
6. Use the **connection pooling** URI (port 6543) for `DATABASE_URL`.

## GitHub Actions (daily agent)

Add repository secrets: `DATABASE_URL`, `SENDGRID_*`, `APP_SECRET_KEY`.

Workflow: [.github/workflows/daily_agent.yml](.github/workflows/daily_agent.yml) — runs daily at 08:00 UTC and supports manual dispatch.

## Project structure

```
app/              Streamlit UI
backend/          Agent, matching, ingestors, auth
utils/            Config and database
scripts/          Seed and utilities
supabase/         Postgres schema
data/             Local SQLite (gitignored)
```

## Pilot organizations

`scripts/seed_pilot_orgs.py` loads:

- **Deborah's Place** — Chicago homelessness services
- **Green Valley Food Bank** — Minnesota food security

Run the agent and compare dashboards to confirm profile-driven results differ.

## License

MIT — use and adapt freely for nonprofit grant prospecting.
