"""
GrantScout AI — Flask Portal
Phase 7: Flask web application replacing Streamlit
Run: python portal/flask_app.py
Opens at: http://localhost:5000
"""

import os
import sys
import json
import csv
import glob
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, Response
)

# ── Path setup ────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# ── Flask app ─────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=str(ROOT / "portal" / "templates"),
    static_folder=str(ROOT / "portal" / "static"),
)
app.secret_key = os.getenv("SECRET_KEY", "grantscout-dev-secret-change-in-production")

# ── Database imports (wrapped to handle table conflicts) ──
try:
    from database.db import SessionLocal, create_tables
    from portal.models.user import User, UserRole
    from portal.auth.security import hash_password, verify_password
    create_tables()
    DB_AVAILABLE = True
except Exception as e:
    if "already defined" not in str(e):
        print(f"[WARNING] DB setup issue: {e}")
    DB_AVAILABLE = True  # still try to use it

# ── Seed demo users on startup ────────────────────────────
def seed_demo_users():
    """Create demo users if they don't exist."""
    try:
        db = SessionLocal()
        demo_users = [
            {
                "email":     "admin@deborahsplace.org",
                "password":  os.getenv("ADMIN_PASSWORD", "AIforGood2026!"),
                "full_name": "Admin User",
                
                "org_name": "Deborah's Place",
                "role":      UserRole.ADMIN,
                "active":    True,
            },
            {
                "email":     "alex@deborahsplace.org",
                "password":  "demo1234",
                "full_name": "Alex Johnson",
                
                "org_name": "Deborah's Place",
                "role":      UserRole.ADMIN,
                "active":    True,
            },
            {
                "email":     "priya@deborahsplace.org",
                "password":  "demo1234",
                "full_name": "Priya Patel",
                
                "org_name": "Deborah's Place",
                "role":      UserRole.USER,
                "active":    True,
            },
            {
                "email":     "jordan@deborahsplace.org",
                "password":  "demo1234",
                "full_name": "Jordan Williams",
                
                "org_name": "Deborah's Place",
                "role":      UserRole.USER,
                "active":    True,
            },
        ]
        for u in demo_users:
            existing = db.query(User).filter(User.email == u["email"]).first()
            if not existing:
                new_user = User(
                    email           = u["email"],
                    hashed_password = hash_password(u["password"]),
                    full_name       = u["full_name"],
                    org_name        = u["org_name"],
                    role            = u["role"],
                    is_active       = u["active"],
                )
                db.add(new_user)
        db.commit()
        db.close()
    except Exception as e:
        print(f"[WARNING] Could not seed demo users: {e}")

seed_demo_users()

# ── Auth helpers ──────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "email" not in session:
            return redirect(url_for("auth.login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "email" not in session:
            return redirect(url_for("auth.login_page"))
        if session.get("role") != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard.index"))
        return f(*args, **kwargs)
    return decorated

def get_db_user(email):
    """Look up a user from the database."""
    try:
        db   = SessionLocal()
        user = db.query(User).filter(User.email == email.lower().strip()).first()
        db.close()
        return user
    except Exception:
        return None

# ── CSV / data helpers ────────────────────────────────────
def load_latest_csv():
    """Load the most recent CSV from outputs/deborahs_place/"""
    pattern = str(ROOT / "outputs" / "deborahs_place" / "*" / "*.csv")
    files   = sorted(glob.glob(pattern))
    if not files:
        return []
    latest = files[-1]
    rows   = []
    try:
        with open(latest, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Sanitize award range backticks
                if "Award Range" in row:
                    row["Award Range"] = row["Award Range"].replace("`", "$").replace("$$", "$")
                # Cap score at 5.0
                if "Final Score" in row:
                    try:
                        row["Final Score"] = str(min(float(row["Final Score"]), 5.0))
                    except (ValueError, TypeError):
                        row["Final Score"] = "0"
                rows.append(row)
    except Exception as e:
        print(f"[WARNING] Could not load CSV: {e}")
    return rows

def is_cold_lead(grant):
    """Return True if this grant should be hidden from the main pipeline."""
    if grant.get("Data Source", "") == "Manual":
        return False
    dq     = (grant.get("Disqualifying Factors", "") or "").lower()
    method = (grant.get("Application Method",   "") or "").lower()
    cold_phrases = [
        "unsolicited applications declined",
        "does not accept unsolicited",
        "by invitation only",
        "invitation only",
    ]
    return any(p in dq or p in method for p in cold_phrases)

def categorize_grants(grants):
    """Split grants into act_now, coming_up, archival, and cold buckets."""
    act_now   = []
    coming_up = []
    archival  = []
    cold      = 0

    for g in grants:
        if is_cold_lead(g):
            cold += 1
            continue
        try:
            days = int(float(g.get("Days Remaining", 999)))
        except (ValueError, TypeError):
            days = 999

        if days <= 30:
            act_now.append(g)
        elif days <= 60:
            coming_up.append(g)
        else:
            archival.append(g)

    # Sort each bucket by score descending
    def by_score(g):
        try:
            return -float(g.get("Final Score", 0))
        except (ValueError, TypeError):
            return 0

    act_now.sort(key=by_score)
    coming_up.sort(key=by_score)
    archival.sort(key=by_score)

    return act_now, coming_up, archival, cold

def compute_stats(act_now, coming_up, archival, all_grants):
    """Compute dashboard stat card values."""
    all_visible = act_now + coming_up + archival
    scores = []
    for g in all_visible:
        try:
            scores.append(float(g.get("Final Score", 0)))
        except (ValueError, TypeError):
            pass
    avg = round(sum(scores) / len(scores), 1) if scores else 0.0
    return {
        "hot_leads":  len(act_now),
        "warm_leads": len(coming_up),
        "archival":   len(archival),
        "avg_score":  avg,
    }

def load_org_profile():
    """Load org profile from JSON."""
    path = ROOT / "profiles" / "deborah_place.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:
        return {}

def get_last_checked():
    """Return a human-readable 'last checked' string."""
    try:
        from agent.state import AgentState
        state    = AgentState()
        last_run = state.get_last_run()
        if last_run:
            dt   = datetime.fromisoformat(last_run)
            diff = datetime.now() - dt
            mins = int(diff.total_seconds() / 60)
            if mins < 2:   return "just now"
            if mins < 60:  return f"{mins} minutes ago"
            hrs = mins // 60
            if hrs < 24:   return f"{hrs} hour{'s' if hrs != 1 else ''} ago"
            days = hrs // 24
            return f"{days} day{'s' if days != 1 else ''} ago"
    except Exception:
        pass
    return "recently"

def get_alert_count():
    try:
        from learning.learning_log import LearningLog
        log   = LearningLog()
        stats = log.get_stats()
        return stats.get("total_changes", 0)
    except Exception:
        return 0

# ── Context processor — injects vars into every template ──
@app.context_processor
def inject_globals():
    return {
        "last_checked": get_last_checked(),
        "alert_count":  get_alert_count(),
        "alerts":       [],          # populated per-route if needed
        "active_page":  "",          # overridden per blueprint
    }

# ============================================================
# AUTH ROUTES
# ============================================================
from flask import Blueprint
auth = Blueprint("auth", __name__, url_prefix="/auth")

@auth.route("/login", methods=["GET"])
def login_page():
    if "email" in session:
        return redirect(url_for("dashboard.index"))
    error = request.args.get("error")
    return render_template("login.html", error=error)

@auth.route("/login", methods=["POST"])
def login():
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    user = get_db_user(email)
    if user and verify_password(password, user.hashed_password) and user.is_active:
        session["email"]     = user.email
        session["full_name"] = user.full_name
        session["role"]      = user.role.value
        session["title"]     = ""
        return redirect(url_for("dashboard.index"))

    return render_template("login.html", error="Invalid email or password.")

@auth.route("/signup", methods=["POST"])
def signup():
    data = request.get_json() or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role_str = data.get("role", "user")

    if not email or not password:
        return jsonify({"success": False, "error": "Email and password are required."})

    try:
        db       = SessionLocal()
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            db.close()
            return jsonify({"success": False, "error": "An account with that email already exists."})

        role = UserRole.ADMIN if role_str == "admin" else UserRole.USER
        # Admins are active immediately; basic users need approval
        is_active = (role == UserRole.ADMIN)

        new_user = User(
            email           = email,
            hashed_password = hash_password(password),
            full_name       = data.get("full_name", "").strip(),
            title           = data.get("title", "").strip(),
            org_name        = data.get("organization", "").strip(),
            role            = role,
            is_active       = is_active,
        )
        db.add(new_user)
        db.commit()
        db.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@auth.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login_page"))

app.register_blueprint(auth)

# ============================================================
# DASHBOARD ROUTES
# ============================================================
dashboard_bp = Blueprint("dashboard", __name__)

@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
@login_required
def index():
    grants                        = load_latest_csv()
    act_now, coming_up, archival, cold = categorize_grants(grants)
    stats                         = compute_stats(act_now, coming_up, archival, grants)

    return render_template(
        "dashboard.html",
        active_page      = "dashboard",
        act_now_grants   = act_now,
        coming_up_grants = coming_up,
        archival_grants  = archival,
        cold_leads_count = cold,
        stats            = stats,
    )

app.register_blueprint(dashboard_bp)

# ============================================================
# PROFILE ROUTES
# ============================================================
profile_bp = Blueprint("profile", __name__, url_prefix="/profile")

@profile_bp.route("/")
@login_required
def index():
    raw     = load_org_profile()
    profile = {
        "org_name":          raw.get("name", raw.get("org_name", "")),
        "mission":           raw.get("mission_statement", raw.get("mission", "")),
        "funding_needs":     raw.get("funding_needs", ""),
        "program_areas":     raw.get("program_areas", []),
        "geographic_focus":  raw.get("geographic_focus", ""),
        "budget_range":      raw.get("budget_range", ""),
        "populations_served": raw.get("populations_served", ""),
    }
    return render_template("profile.html", active_page="profile", profile=profile)

@profile_bp.route("/save", methods=["POST"])
@login_required
def save():
    data = request.get_json() or {}
    path = ROOT / "profiles" / "deborah_place.json"
    try:
        # Load existing profile to preserve fields we don't edit here
        existing = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)

        # Update editable fields
        areas_raw = data.get("program_areas", "")
        areas     = [a.strip() for a in areas_raw.split(",") if a.strip()]

        existing.update({
            "name":              data.get("org_name", ""),
            "mission_statement": data.get("mission", ""),
            "funding_needs":     data.get("funding_needs", ""),
            "program_areas":     areas,
            "geographic_focus":  data.get("geographic_focus", ""),
            "budget_range":      data.get("budget_range", ""),
            "populations_served": data.get("populations_served", ""),
        })

        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

app.register_blueprint(profile_bp)

# ============================================================
# GRANTS ROUTES
# ============================================================
grants_bp = Blueprint("grants", __name__, url_prefix="/grants")

@grants_bp.route("/add", methods=["GET"])
@login_required
def add():
    return render_template("add_grant.html", active_page="add_grant")

@grants_bp.route("/add", methods=["POST"])
@login_required
def add_post():
    data = request.get_json() or {}
    try:
        # Build a row matching our CSV schema
        today    = datetime.now().strftime("%Y-%m-%d")
        deadline_str = data.get("deadline", "")
        days_remaining = 999
        if deadline_str:
            try:
                dl   = datetime.strptime(deadline_str, "%Y-%m-%d")
                days_remaining = (dl - datetime.now()).days
            except ValueError:
                pass

        row = {
            "Funder Name":              data.get("funder_name", ""),
            "Program Name":             data.get("program_name", ""),
            "Description":              data.get("description", ""),
            "Application Deadline":     deadline_str,
            "Days Remaining":           str(days_remaining),
            "Award Range":              data.get("award_range", ""),
            "Award Min":                str(data.get("award_min") or ""),
            "Award Max":                str(data.get("award_max") or ""),
            "Application URL":          data.get("application_url", ""),
            "Eligibility Requirements": data.get("eligibility", ""),
            "Data Source":              "Manual",
            "Source URL":               data.get("application_url", ""),
            "Date Found":               today,
            "Organization":             "Deborah's Place",
            "Final Score":              "0",
            "Composite Score":          "0",
            "Geographic Focus":         data.get("location", ""),
            "Disqualifying Factors":    "",
            "Score: Geographic Alignment": "0",
            "Score: Population Alignment": "0",
            "Score: Budget Fit":           "0",
            "Score: Timeline Feasibility": "0",
            "Reason: Geographic":  "",
            "Reason: Population":  "",
            "Reason: Budget":      "",
            "Reason: Timeline":    "",
            "Recommended Next Action": "Review manually added grant",
            "Prior Funder":       "No",
            "Program Officer":    "",
            "Funder Website":     "",
            "Application Method": data.get("application_url", ""),
            "Focus Areas":        "",
            "Completeness Notes": "Manually added",
        }

        # Append to the latest CSV or create a new one
        output_dir = ROOT / "outputs" / "deborahs_place" / today
        output_dir.mkdir(parents=True, exist_ok=True)

        # Find existing CSV for today or create one
        existing_files = list(output_dir.glob("*.csv"))
        if existing_files:
            csv_path = existing_files[0]
            # Append row
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                writer.writerow(row)
        else:
            csv_path = output_dir / f"grant_prospects_{today}_manual.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                writer.writeheader()
                writer.writerow(row)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

app.register_blueprint(grants_bp)

# ============================================================
# USERS ROUTES  (admin only)
# ============================================================
users_bp = Blueprint("users", __name__, url_prefix="/users")

@users_bp.route("/")
@admin_required
def index():
    try:
        db      = SessionLocal()
        pending = db.query(User).filter(User.is_active == False).all()
        active  = db.query(User).filter(User.is_active == True).all()
        db.close()
        return render_template(
            "users.html",
            active_page  = "users",
            pending_users = pending,
            active_users  = active,
        )
    except Exception as e:
        flash(f"Could not load users: {e}", "error")
        return redirect(url_for("dashboard.index"))

@users_bp.route("/approve/<int:user_id>", methods=["POST"])
@admin_required
def approve(user_id):
    try:
        db   = SessionLocal()
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.is_active = True
            db.commit()
        db.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@users_bp.route("/deny/<int:user_id>", methods=["POST"])
@admin_required
def deny(user_id):
    try:
        db   = SessionLocal()
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            db.delete(user)
            db.commit()
        db.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@users_bp.route("/add", methods=["POST"])
@admin_required
def add_user():
    data = request.get_json() or {}
    try:
        db       = SessionLocal()
        email    = (data.get("email") or "").strip().lower()
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            db.close()
            return jsonify({"success": False, "error": "Email already exists."})

        role     = UserRole.ADMIN if data.get("role") == "admin" else UserRole.USER
        new_user = User(
            email           = email,
            hashed_password = hash_password(data.get("password", "changeme")),
            full_name       = (data.get("full_name") or "").strip(),
            role            = role,
            is_active       = True,
        )
        db.add(new_user)
        db.commit()
        db.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@users_bp.route("/role/<int:user_id>", methods=["POST"])
@admin_required
def change_role(user_id):
    data = request.get_json() or {}
    try:
        db   = SessionLocal()
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.role = UserRole.ADMIN if data.get("role") == "admin" else UserRole.USER
            db.commit()
        db.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@users_bp.route("/delete/<int:user_id>", methods=["POST"])
@admin_required
def delete_user(user_id):
    try:
        db   = SessionLocal()
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.email != session.get("email"):
            db.delete(user)
            db.commit()
        db.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

app.register_blueprint(users_bp)

# ============================================================
# API ROUTES
# ============================================================
api_bp = Blueprint("api", __name__, url_prefix="/api")

@api_bp.route("/run-agent", methods=["POST"])
@login_required
def run_agent():
    """Stream agent progress back to the browser."""
    def generate():
        try:
            from agent.profile import OrgProfile
            from agent.loop   import AgentLoop
            from output.formatter import ResultFormatter
            from output.exporter  import ResultExporter

            profile   = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
            loop      = AgentLoop(profile)
            formatter = ResultFormatter(profile)
            exporter  = ResultExporter(profile)

            yield "Starting agent...\n"
            results = loop.run(max_queries=5)
            yield f"Query complete. Found {len(results)} results.\n"

            if results:
                yield "Scoring results...\n"
                formatted = formatter.format_all(results)
                yield "Saving results...\n"
                exporter.export_csv(formatted)
                exporter.export_excel(formatted)
                yield f"Done. {len(formatted)} grants saved.\n"
            else:
                yield "No new grants found this run.\n"
        except Exception as e:
            yield f"Error: {e}\n"

    return Response(generate(), mimetype="text/plain")

@api_bp.route("/search-990", methods=["POST"])
@login_required
def search_990():
    try:
        from agent.profile import OrgProfile
        from tools.form_990 import Form990Tool
        profile = OrgProfile.from_json(ROOT / "profiles" / "deborah_place.json")
        tool    = Form990Tool()
        results = tool.search(profile.name)
        return jsonify({"success": True, "message": f"Found {len(results)} archival sources.", "count": len(results)})
    except Exception as e:
        return jsonify({"success": False, "message": f"Archival search failed: {e}"})

@api_bp.route("/switch-role", methods=["POST"])
@login_required
def switch_role():
    data = request.get_json() or {}
    role = data.get("role", "user")
    if role in ("admin", "user"):
        session["role"] = role
    return jsonify({"success": True})

@api_bp.route("/simulate-alert", methods=["POST"])
@login_required
def simulate_alert():
    grant_names = [
        "Women's Housing Initiative",
        "Community Safety Net Fund",
        "Economic Mobility Grant",
        "Mental Health Access Program",
    ]
    import random
    name = random.choice(grant_names)
    return jsonify({"success": True, "message": f"Alert simulated: {name}"})

app.register_blueprint(api_bp)

# ============================================================
# ERROR HANDLERS
# ============================================================
@app.errorhandler(404)
def not_found(e):
    if "email" in session:
        flash("Page not found.", "error")
        return redirect(url_for("dashboard.index"))
    return redirect(url_for("auth.login_page"))

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    print("\n" + "="*50)
    print("  GrantScout AI — Flask Portal")
    print("  http://localhost:5000")
    print("  Admin: admin@deborahsplace.org / AIforGood2026!")
    print("="*50 + "\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
