# .\tools\tailwindcss.exe -i static\css\input.css -o static\css\output.css --watch

from dotenv import load_dotenv
import os

# Must run before local imports -- auth.py reads env vars at import time, not request time.
root_dir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(root_dir, ".env"))

import atexit
import logging
import logging.handlers
from flask import Flask, jsonify, render_template, request, g, redirect, url_for
from werkzeug.exceptions import HTTPException
from routes.requests import requests_bp
from routes.pages import pages_bp
from routes.assets import assets_bp
from routes.members import members_bp
from routes.staff import staff_bp
from routes.auth import auth_bp
from routes.transactions import transactions_bp
from database import init_db, get_db_connection
from services.seed_service import seed_initial_admin
from services.checkout_service import cleanup_stale_checkout_sessions
from services.auth_service import cleanup_expired_sessions, require_session
from utils.formatters import full_name, humanize_label
from services.settings_service import INDEFINITE_LOCK_UNTIL, PERMANENT_ASSIGNED_END, get_feature_flags
from services.approvals_service import get_pending_approvals_count
from services.admin_deactivation_service import is_eligible_reviewer
from apscheduler.schedulers.background import BackgroundScheduler
from extensions import limiter
from routes.stats import stats_bp
from routes.setup import setup_bp
from routes.notifications import notifications_bp
from routes.feedback import feedback_bp, get_open_feedback_count
from services.loan_service import sweep_overdue_members
from services.notification_service import (
    sync_loan_notifications,
    get_notification_bell_context,
)
from services.dashboard_service import get_dashboard_context
from services.backup_service import run_scheduled_backup
from services.archive_purge_service import run_scheduled_archive_purge
from routes.dashboard import dashboard_bp
from datetime import datetime


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        # Rotating, not plain -- keeps the log from filling up the Pi's small SD card.
        logging.handlers.RotatingFileHandler(
            os.path.join(root_dir, "toolroom.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
        ),
    ],
)


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

# Room for 5 feedback screenshots at the 5MB cap, plus multipart overhead.
app.config["MAX_CONTENT_LENGTH"] = 26 * 1024 * 1024

# 1hr cache on static files -- balances SD-card re-reads against staleness after a redeploy.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set in .env — required even though sessions are DB-backed, since Flask itself needs it for flash messages and any future signed cookies."
    )


@app.context_processor
def inject_csrf_token():
    conn = get_db_connection()
    token = request.cookies.get("session_token")
    if not token:
        # Needed even here since base.html's nav guards run on the logged-out /login page too.
        feature_flags = get_feature_flags(conn)
        conn.close()
        return {"csrf_token": "", "staff_first_name": "", "feature_flags": feature_flags}

    session = conn.execute(
        "SELECT csrf_token FROM sessions WHERE token = ?", (token,)
    ).fetchone()

    staff_member = conn.execute(
        """
        SELECT staff.id AS staff_id, staff.role,
               staff.notify_due_soon, staff.notify_overdue, staff.notify_requests,
               members.first_name, members.last_name,
               members.member_type, members.batch,
               departments.name AS department_name
        FROM sessions
        JOIN staff ON staff.id = sessions.staff_id
        JOIN members ON members.id = staff.member_id
        LEFT JOIN departments ON departments.id = members.department_id
        WHERE sessions.token = ?
        """,
        (token,),
    ).fetchone()

    # Skip the notifications query for anonymous requests -- feeds base.html's bell dropdown.
    notifications, unread_notifications_count = (
        get_notification_bell_context(
            conn, staff_member["staff_id"], staff_member["role"], staff_member
        )
        if session and staff_member
        else ([], 0)
    )
    # Sidebar "Approvals" badge count -- see get_pending_approvals_count for scoping rules.
    pending_approvals_count = (
        get_pending_approvals_count(conn, staff_member["staff_id"], staff_member["role"])
        if staff_member
        else 0
    )
    # Badge for the admin-only System sidebar link.
    open_feedback_count = (
        get_open_feedback_count(conn)
        if staff_member and staff_member["role"] == "admin"
        else 0
    )
    # Available in every template so base.html/pages can gate nav links and widgets on their own.
    feature_flags = get_feature_flags(conn)
    conn.close()

    return {
        "csrf_token": session["csrf_token"] if session else "",
        "feature_flags": feature_flags,
        "staff_first_name": staff_member["first_name"] if staff_member else "",
        "staff_full_name": (
            full_name(staff_member["first_name"], staff_member["last_name"])
            if staff_member
            else ""
        ),
        "notifications": notifications,
        "unread_notifications_count": unread_notifications_count,
        "pending_approvals_count": pending_approvals_count,
        "open_feedback_count": open_feedback_count,
        # For the "My Account" modal -- comes from the linked member record, not the staff table.
        "staff_member_type": staff_member["member_type"] if staff_member else "",
        "staff_batch": staff_member["batch"] if staff_member else None,
        "staff_department_name": staff_member["department_name"] if staff_member else "",
    }


app.jinja_env.globals["full_name"] = full_name
app.jinja_env.globals["is_eligible_reviewer"] = is_eligible_reviewer

# Exposes the indefinite-lock sentinel to templates so they can tell it apart from a timed lock.
app.jinja_env.globals["INDEFINITE_LOCK_UNTIL"] = INDEFINITE_LOCK_UNTIL

# Same idea for the "Permanent" assignment sentinel used in staff templates/modals.
app.jinja_env.globals["PERMANENT_ASSIGNED_END"] = PERMANENT_ASSIGNED_END


limiter.init_app(app)

app.register_blueprint(requests_bp, url_prefix="/api/v1")
app.register_blueprint(assets_bp, url_prefix="/api/v1")
app.register_blueprint(members_bp, url_prefix="/api/v1")
app.register_blueprint(staff_bp, url_prefix="/api/v1")
app.register_blueprint(auth_bp, url_prefix="/api/v1")
app.register_blueprint(transactions_bp, url_prefix="/api/v1")
app.register_blueprint(stats_bp, url_prefix="/api/v1")
app.register_blueprint(setup_bp, url_prefix="/api/v1")
app.register_blueprint(notifications_bp, url_prefix="/api/v1")
app.register_blueprint(feedback_bp, url_prefix="/api/v1")
app.register_blueprint(dashboard_bp, url_prefix="/api/v1")
app.register_blueprint(pages_bp)


@app.after_request
def cache_uploaded_files_aggressively(response):
    """Uploaded files get a fresh filename every time, so caching them long-term is safe."""
    if request.path.startswith("/static/uploads/"):
        response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return response


# # # ERROR HANDLERS ---------------------------------------------------------


@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """Reformats Flask/Werkzeug's built-in HTTP errors (404, 405, 429, ...) as JSON."""
    response = jsonify({"error": e.description})
    response.status_code = e.code
    return response


@app.errorhandler(Exception)
def handle_unexpected_exception(e):
    """Catch-all for real bugs (unhandled exceptions). Logs full traceback, hides details from the client."""
    app.logger.exception("Unhandled exception")
    response = jsonify({"error": "An unexpected error occurred"})
    response.status_code = 500
    return response


init_db()
seed_initial_admin()


scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_stale_checkout_sessions, "interval", minutes=5)
scheduler.add_job(cleanup_expired_sessions, "interval", minutes=30)
scheduler.add_job(sweep_overdue_members, "interval", minutes=15)
scheduler.add_job(sync_loan_notifications, "interval", minutes=15)
# Polls every 30 min; the function itself decides whether a backup is actually due.
scheduler.add_job(run_scheduled_backup, "interval", minutes=30)
# Hourly is fine -- retention windows are day-granularity, not minute.
scheduler.add_job(run_scheduled_archive_purge, "interval", hours=1)

debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"


if not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    scheduler.start()

    atexit.register(lambda: scheduler.shutdown())


@app.route("/")
def home():
    """The dashboard (templates/home.html) -- counts, recent activity, top-borrowed
    assets, quick actions. Query layer lives in services/dashboard_service.py."""
    conn = get_db_connection()
    staff, error = require_session(conn)
    if error:
        conn.close()
        return redirect(url_for("pages.login_page"))
    # Send the temp setup admin to the wizard instead of the real dashboard.
    if staff["is_system_seeded"]:
        conn.close()
        return redirect(url_for("pages.setup_page"))
    dashboard_context = get_dashboard_context(conn)
    conn.close()
    return render_template("home.html", staff=staff, **dashboard_context)


@app.route("/health")
@limiter.exempt
def health():
    """Confirms the app is up and can reach the DB. No auth needed -- meant for a watchdog script."""
    try:
        conn = get_db_connection()
        conn.execute("SELECT 1")
        conn.close()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        app.logger.error(f"Health check failed: {e}")
        return jsonify({"status": "error", "detail": "database unavailable"}), 503


def format_date(value, format="%b %d, %Y %I:%M %p"):
    """Jinja filter: formats dates, handling datetime objects and ISO strings."""
    if value is None:
        return "—"

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return value

    if hasattr(value, "strftime"):
        return value.strftime(format)

    return value


app.jinja_env.filters["format_date"] = format_date
# Also expose as a global, for calling it directly instead of via the | filter.
app.jinja_env.globals["format_date"] = format_date

# Turns a snake_case DB value into a display label, e.g. 'for_repair' -> 'For Repair'.
app.jinja_env.filters["humanize"] = humanize_label


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", 5000))
    app.run(host=host, port=port, debug=debug_mode)
