# Grant Prospecting AI Agent
### AI for Good — P33 Chicago × United Way of Metro Chicago × Gary Comer Youth Center

> An autonomous AI-powered grant prospecting tool that identifies current, open, and actionable private funding opportunities for nonprofit organizations — built for **Deborah's Place** and designed for any nonprofit to use.

---

## The Problem This Solves

Nonprofit development teams spend enormous amounts of time manually searching for grant opportunities across dozens of disconnected databases, foundation websites, philanthropy newsletters, and IRS records. The process is fragmented, heavily manual, and dependent on institutional knowledge that does not transfer easily between staff members.

This tool changes that. It works continuously on a nonprofit's behalf — searching, filtering, scoring, and delivering only the opportunities that are genuinely worth pursuing, explained in plain English, with exactly what to do next.

---

## What the Tool Does

The agent runs three interlocking cycles automatically:

| Cycle | Frequency | What It Does |
|---|---|---|
| Monitoring | Daily | Checks all known sources for new open opportunities |
| Discovery | Weekly | Finds new funding sources not yet on the radar |
| Relationship Mapping | Monthly | Builds a living intelligence map of the funding landscape |

For every opportunity it surfaces, it delivers:

**The Who** — the specific funding organization and program behind this specific open opportunity

**The How** — exact eligibility requirements, application deadline, award range, and the precise steps to apply right now

Every result is scored on a 1–5 qualification matrix across four criteria — geographic alignment, population served alignment, budget fit (weighted most heavily), and timeline feasibility — with a written explanation for every score.

---

## Built For

**Deborah's Place** — a Chicago nonprofit providing housing, support services, and advocacy for women experiencing homelessness. Deborah's Place served 688 women last year and carries a private grant revenue goal of approximately $910,000 annually.

**And any nonprofit** — the tool is designed so any organization can be onboarded by filling out a single JSON profile. The engine never changes. Only the profile changes.

---

## Key Features

- **Autonomous operation** — three cycles run on schedule with no manual intervention
- **AI-powered scoring** — Claude AI evaluates every opportunity across four criteria with plain-English explanations
- **Federal grant exclusion** — toggle to deprioritize federal funding in favor of private foundations
- **Self-learning** — when staff find a grant the agent missed, they submit it and the agent analyzes why it was missed, updates its own watch list, and never misses that source again
- **Web portal** — clean browser-based interface built in Streamlit. No terminal required for nonprofit staff
- **Role-based access** — Admin and User roles with secure login
- **Export ready** — ranked results exported to CSV and Excel after every run
- **Multi-org ready** — any nonprofit can use this tool by providing their own profile JSON

---

## Tech Stack

| Library | Purpose |
|---|---|
| Python 3.11+ | Core language |
| Streamlit | Browser-based UI — no frontend code required |

The app runs entirely from a single self-contained entry point (`main.py`) with
one dependency. There are no API keys, no database, and no cryptographic secrets
to manage.

---

## Project Architecture

```
grant-prospector/
├── agent/              # Core agent — profile, keyword mapper, loop, scheduler
├── cycles/             # Three operating cycles — monitoring, discovery, relationship map
├── learning/           # Autonomous learning loop — feedback, gap analysis, self-update
├── tools/              # Search tool modules — web search, IRS 990, Grants.gov
├── scoring/            # Eligibility filter and AI scoring engine
├── output/             # Formatter and CSV/Excel exporter
├── portal/             # Web portal — Streamlit UI and FastAPI backend
├── database/           # Database setup and models
├── profiles/           # Org profiles — Deborah's Place + blank template
└── run_agent.py        # CLI entry point — one command runs the full pipeline
```

---

## The Three Operating Cycles

### Cycle 1 — Discovery (Weekly)
Actively finds NEW funding sources not yet on the agent's watch list by mining IRS 990 data, tracking co-funder relationships, and monitoring philanthropy news for newly announced initiatives.

### Cycle 2 — Monitoring (Daily)
Checks every source in the active watch list for new open opportunities. Runs the full qualification pipeline — eligibility filter → AI scorer → ranked output. Only opportunities with active deadlines pass through.

### Cycle 3 — Relationship Mapping (Monthly)
Builds a living intelligence map of the funding landscape. Identifies warm cultivation targets — foundations with strong mission alignment that have no open RFP yet but whose giving history suggests they should be approached now. Surfaces strategic timing signals so development teams can reach out before cycles open.

---

## The Autonomous Learning Loop

When staff find a grant the agent missed they submit it through the portal. The agent:

1. Analyzes where the grant came from and why it was missed
2. Identifies the gap — source not monitored, keyword gap, search too narrow, or site structure issue
3. Automatically updates its own watch list and search patterns
4. Notifies the Admin describing exactly what it changed and why
5. Logs every change permanently for the audit trail

The agent gets smarter with every correction. No developer intervention required.

---

## Priority Private Funding Sources

The tool is configured to prioritize private foundations given the current federal funding environment. Chicago-based funders monitored from day one include:

- **MacArthur Foundation** — co-chair of the Resilient Chicago Fund, increased grantmaking 2025–2026
- **Chicago Community Trust** — $6B+ assets, co-leads Resilient Chicago Fund
- **Polk Bros. Foundation** — $25M+ annually to nearly 400 Chicago nonprofits
- **Chicago Foundation for Women** — prior giving relationship with Deborah's Place
- **Joyce Foundation** — economic mobility and workforce development alignment

---

## Getting Started

### Requirements
- Python 3.11+

No API keys, no database setup, and no cryptographic secrets are required to run the app.

### Setup

```bash
# Clone the repository
git clone https://github.com/78k6fcrsh4-rgb/GProspect.git
cd GProspect

# Install the single dependency
pip install -r requirements.txt
```

### Run the App

One command starts the full web app in your browser:

```bash
streamlit run main.py
```

Streamlit opens `http://localhost:8501` automatically. Sign in with the demo credentials shown on the login screen (`alex@deborahsplace.org` / `demo1234`). No terminal knowledge required after this point.

---

## Onboarding a New Nonprofit

1. Copy `profiles/org_profile_template.json`
2. Fill in the organization's mission, programs, geography, budget range, and known funders
3. Save as `profiles/your_org_name.json`
4. Run: `python3 run_agent.py --profile profiles/your_org_name.json`

The agent automatically configures all three cycles, the scoring matrix, and the search patterns for the new organization. No code changes required.

---

## Development Status

| Phase | Description | Status |
|---|---|---|
| 1 | Foundation — org profile, keyword mapper | ✅ Complete |
| 2 | Data layer — search tools, tool registry | ✅ Complete |
| 3 | Agent core — scoring, eligibility, CLI | ✅ Complete |
| 4 | Automation — three operating cycles | ✅ Complete |
| 5 | Learning loop — autonomous self-improvement | ✅ Complete |
| 6 | Web portal — login, results, admin dashboard | ✅ Complete |
| 7 | Full product — Candid integration, LOI drafts, test suite | 🔄 In Progress |

---

## About AI for Good

The AI for Good initiative is a partnership between **P33 Chicago**, **United Way of Metro Chicago**, and the **Gary Comer Youth Center**, operating out of Exchange Chicago. The program places Exchange graduates — IT professionals who grew up and live in Chicago — in real-world engagements with nonprofit organizations.

Nonprofits receive digital solutions at no cost. Graduates gain hands-on experience building tools that make a real difference in their community.

---

## Built By

Developed by **Orrin Murray** and the AI for Good team as part of the Exchange Chicago program.

Project initiated: April 2026
First working agent: May 2026
Target delivery to Deborah's Place: July–August 2026

---

## License

Built for nonprofit use. Free to use, adapt, and extend for any nonprofit grant prospecting application.