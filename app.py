# .\tools\tailwindcss.exe -i static\css\input.css -o static\css\output.css --watch

from dotenv import load_dotenv
import os
import atexit
import logging
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
from utils.formatters import full_name
from services.settings_service import INDEFINITE_LOCK_UNTIL
from services.approvals_service import get_pending_approvals_count
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
from routes.dashboard import dashboard_bp
from datetime import datetime

root_dir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(root_dir, ".env"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(root_dir, "toolroom.log")),
    ],
)


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

# Was 1 MB, which -- since it's a hard Flask-level cap applied before any
# route runs -- already undercut MAX_IMAGE_SIZE_BYTES (5MB, utils/uploads.py)
# for any single-image upload. Raised to cover the new feedback-screenshot
# upload (routes/feedback.py), which allows up to MAX_FEEDBACK_IMAGES (5)
# images in one multipart request; 26 MB is 5 images at the 5MB cap plus
# headroom for multipart boundaries/form fields.
app.config["MAX_CONTENT_LENGTH"] = 26 * 1024 * 1024

if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set in .env — required even though sessions are DB-backed, since Flask itself needs it for flash messages and any future signed cookies."
    )


@app.context_processor
def inject_csrf_token():
    conn = get_db_connection()
    token = request.cookies.get("session_token")
    if not token:
        conn.close()
        return {"csrf_token": "", "staff_first_name": ""}

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

    # Only bother querying notifications for an actual logged-in session --
    # an anonymous request (e.g. /login) has no session row and no bell to
    # populate. Feeds the sidebar bell dropdown in base.html; the full
    # /notifications page re-queries this itself with its own pagination.
    notifications, unread_notifications_count = (
        get_notification_bell_context(
            conn, staff_member["staff_id"], staff_member["role"], staff_member
        )
        if session and staff_member
        else ([], 0)
    )
    # Same idea for the sidebar's "Approvals" badge -- see
    # get_pending_approvals_count's docstring for the scoping rules.
    pending_approvals_count = (
        get_pending_approvals_count(conn, staff_member["staff_id"], staff_member["role"])
        if staff_member
        else 0
    )
    # Badges the System sidebar link (admin-only, see templates/base.html)
    # -- only worth querying for the role that can ever see that link.
    open_feedback_count = (
        get_open_feedback_count(conn)
        if staff_member and staff_member["role"] == "admin"
        else 0
    )
    conn.close()

    return {
        "csrf_token": session["csrf_token"] if session else "",
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
        # Feeds the extra rows in the "My Account" modal (templates/base.html)
        # -- a staff account is always tied to a member record (staff.member_id),
        # so these come from there, not the staff table itself.
        "staff_member_type": staff_member["member_type"] if staff_member else "",
        "staff_batch": staff_member["batch"] if staff_member else None,
        "staff_department_name": staff_member["department_name"] if staff_member else "",
    }


app.jinja_env.globals["full_name"] = full_name

# Lets staff/_table.html and staff/_profile_modal.html tell an indefinite
# manual lock (services.settings_service.INDEFINITE_LOCK_UNTIL) apart from
# an ordinary timed one without duplicating that sentinel string in Jinja.
app.jinja_env.globals["INDEFINITE_LOCK_UNTIL"] = INDEFINITE_LOCK_UNTIL


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


# # # ERROR HANDLERS ---------------------------------------------------------


@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """Catches Flask/Werkzeug's built-in HTTP errors — 404 Not Found, 405
    Method Not Allowed, 429 Too Many Requests (from Flask-Limiter), and
    similar — and reformats them as JSON instead of Werkzeug's default HTML
    error page. e.description already has a reasonable human-readable
    message for all of these (e.g. Flask-Limiter fills it in with the
    specific limit that was hit)."""
    response = jsonify({"error": e.description})
    response.status_code = e.code
    return response


@app.errorhandler(Exception)
def handle_unexpected_exception(e):
    """Last-resort catch-all for anything that isn't an HTTPException — i.e.
    an actual bug (unhandled Python exception) rather than an expected error
    condition. Logs the full traceback server-side (so it's still
    debuggable) but never leaks internal details to the client."""
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

debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"


if not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    scheduler.start()

    atexit.register(lambda: scheduler.shutdown())


@app.route("/")
def home():
    """The Dashboard -- templates/home.html is the file (kept at its
    existing name/route rather than moved to a new dashboard.html), but
    everything it renders is the one-page operational overview: live
    inventory/member/loan counts, a recent-activity feed, top-borrowed
    assets, and quick actions. See services/dashboard_service.py for the
    query layer behind it."""
    conn = get_db_connection()
    staff, error = require_session(conn)
    if error:
        conn.close()
        return redirect(url_for("pages.login_page"))
    dashboard_context = get_dashboard_context(conn)
    conn.close()
    return render_template("home.html", staff=staff, **dashboard_context)


@app.route("/health")
@limiter.exempt
def health():
    """Confirms the app is up AND can actually reach the database — an
    app that's technically running but can't talk to a locked/corrupted
    DB file is still broken. No auth required, since this is meant for a
    watchdog script or quick manual check, not a staff-facing route.
    Exempt from rate limiting since a watchdog may poll it frequently."""
    try:
        conn = get_db_connection()
        conn.execute("SELECT 1")
        conn.close()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        app.logger.error(f"Health check failed: {e}")
        return jsonify({"status": "error", "detail": "database unavailable"}), 503


def format_date(value, format="%b %d, %Y %I:%M %p"):
    """
    Custom Jinja filter to format dates.
    Handles datetime objects, ISO string formats, or returns the original value if parsing fails.
    """
    if value is None:
        return "—"

    if isinstance(value, str):
        try:
            # Attempts to parse ISO-formatted string dates
            value = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return value

    if hasattr(value, "strftime"):
        return value.strftime(format)

    return value


# Register the custom filter with Jinja
app.jinja_env.filters["format_date"] = format_date
# Optional: Register in globals if you access it directly as a function in templates
app.jinja_env.globals["format_date"] = format_date


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", 5000))
    app.run(host=host, port=port, debug=debug_mode)
