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
from apscheduler.schedulers.background import BackgroundScheduler
from extensions import limiter
from routes.stats import stats_bp
from routes.setup import setup_bp
from services.loan_service import sweep_overdue_members

root_dir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(root_dir, ".env"))


# Configured once, here, before anything else in the app logs anything
# (init_db() and seed_initial_admin() both run at import time below).
# StreamHandler â†’ console, which systemd/journald captures automatically
# on the Pi. FileHandler â†’ a local log file, useful right now on the
# laptop where there's no journald to check.
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

app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB request body cap

if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set in .env â€” required even though sessions are DB-backed, since Flask itself needs it for flash messages and any future signed cookies."
    )


@app.context_processor
def inject_csrf_token():
    conn = get_db_connection()
    token = request.cookies.get("session_token")
    if token:
        session = conn.execute(
            "SELECT csrf_token FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        conn.close()
        return {"csrf_token": session["csrf_token"] if session else ""}
    conn.close()
    return {"csrf_token": ""}


limiter.init_app(app)

app.register_blueprint(requests_bp, url_prefix="/api/v1")
app.register_blueprint(assets_bp, url_prefix="/api/v1")
app.register_blueprint(members_bp, url_prefix="/api/v1")
app.register_blueprint(staff_bp, url_prefix="/api/v1")
app.register_blueprint(auth_bp, url_prefix="/api/v1")
app.register_blueprint(transactions_bp, url_prefix="/api/v1")
app.register_blueprint(stats_bp, url_prefix="/api/v1")
app.register_blueprint(setup_bp, url_prefix="/api/v1")
app.register_blueprint(pages_bp)


# # # ERROR HANDLERS ---------------------------------------------------------
# Every route in this app already returns jsonify(...) for its own business-
# logic errors (missing fields, not found, etc.) â€” these two handlers only
# catch what routes *don't* raise themselves: unmatched URLs (404), wrong
# HTTP method (405), rate limits (429, raised by Flask-Limiter), and any
# genuinely unexpected bug (500). Without these, those cases fall through to
# Flask's default HTML error page, which breaks the "this API always returns
# JSON" contract for anything consuming it (htmx, JS fetch, etc.).


@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """Catches Flask/Werkzeug's built-in HTTP errors â€” 404 Not Found, 405
    Method Not Allowed, 429 Too Many Requests (from Flask-Limiter), and
    similar â€” and reformats them as JSON instead of Werkzeug's default HTML
    error page. e.description already has a reasonable human-readable
    message for all of these (e.g. Flask-Limiter fills it in with the
    specific limit that was hit)."""
    response = jsonify({"error": e.description})
    response.status_code = e.code
    return response


@app.errorhandler(Exception)
def handle_unexpected_exception(e):
    """Last-resort catch-all for anything that isn't an HTTPException â€” i.e.
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

debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"

# Flask's debug reloader spawns a second process â€” only run the scheduler
# in the actual worker process, not the reloader's parent watcher, or
# you'll get every job firing twice.
if not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    scheduler.start()
    # Ensure the background thread stops cleanly on process exit (Ctrl+C,
    # systemd stop, etc.) instead of being left dangling.
    atexit.register(lambda: scheduler.shutdown())


@app.route("/")
def home():
    conn = get_db_connection()
    staff, error = require_session(conn)
    conn.close()
    if error:
        return redirect(url_for("pages.login_page"))
    return render_template("home.html", staff=staff)


@app.route("/health")
@limiter.exempt
def health():
    """Confirms the app is up AND can actually reach the database â€” an
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


@app.route("/test")
def test():
    return render_template("base.html")


# if __name__ == "__main__":
#     app.run(debug=debug_mode)

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", 5000))
    app.run(host=host, port=port, debug=debug_mode)

