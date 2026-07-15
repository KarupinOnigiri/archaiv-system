from dotenv import load_dotenv
import os

root_dir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(root_dir, ".env"))

import logging

logger = logging.getLogger(__name__)

# Configured once, here, before anything else in the app logs anything
# (init_db() and seed_initial_admin() both run at import time below).
# StreamHandler → console, which systemd/journald captures automatically
# on the Pi. FileHandler → a local log file, useful right now on the
# laptop where there's no journald to check.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(root_dir, "toolroom.log")),
    ],
)

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException
from routes.requests import requests_bp
from routes.assets import assets_bp
from routes.members import members_bp
from routes.staff import staff_bp
from routes.auth import auth_bp
from routes.transactions import transactions_bp
from database import init_db
from services.seed_service import seed_initial_admin
from apscheduler.schedulers.background import BackgroundScheduler
from services.checkout_service import cleanup_stale_checkout_sessions
from extensions import limiter
from routes.stats import stats_bp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB request body cap

if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set in .env — required even though sessions are DB-backed, since Flask itself needs it for flash messages and any future signed cookies."
    )

limiter.init_app(app)

app.register_blueprint(requests_bp)
app.register_blueprint(assets_bp)
app.register_blueprint(members_bp)
app.register_blueprint(staff_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(transactions_bp)
app.register_blueprint(stats_bp)


# # # ERROR HANDLERS ---------------------------------------------------------
# Every route in this app already returns jsonify(...) for its own business-
# logic errors (missing fields, not found, etc.) — these two handlers only
# catch what routes *don't* raise themselves: unmatched URLs (404), wrong
# HTTP method (405), rate limits (429, raised by Flask-Limiter), and any
# genuinely unexpected bug (500). Without these, those cases fall through to
# Flask's default HTML error page, which breaks the "this API always returns
# JSON" contract for anything consuming it (htmx, JS fetch, etc.).


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

debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"

# Flask's debug reloader spawns a second process — only run the scheduler
# in the actual worker process, not the reloader's parent watcher, or
# you'll get every job firing twice.
if not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    scheduler.start()


@app.route("/")
def home():
    return "Flask is connected and running."


# if __name__ == "__main__":
#     app.run(debug=debug_mode)

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", 5000))
    app.run(host=host, port=port, debug=debug_mode)
