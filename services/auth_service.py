from functools import wraps
from database import get_db_connection
from flask import jsonify, request, g, redirect
from datetime import datetime, timedelta
from services.settings_service import get_session_idle_minutes, is_feature_enabled, get_feature_label
from urllib.parse import quote
import hmac
import logging

logger = logging.getLogger(__name__)


def require_session(conn):
    token = request.cookies.get("session_token")

    if not token:
        return None, (jsonify({"error": "Not logged in"}), 401)

    session = conn.execute(
        "SELECT * FROM sessions WHERE token = ?", (token,)
    ).fetchone()

    if session is None:
        return None, (jsonify({"error": "Invalid or expired session"}), 401)

    # CSRF check - only required for state-changing methods
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        submitted_csrf = request.headers.get("X-CSRF-Token")
        if not submitted_csrf or not hmac.compare_digest(
            submitted_csrf, session["csrf_token"]
        ):
            return None, (jsonify({"error": "Invalid or missing CSRF token"}), 403)

    now = datetime.now()
    expires_at = datetime.strptime(session["expires_at"], "%Y-%m-%d %H:%M:%S")
    last_active_at = datetime.strptime(session["last_active_at"], "%Y-%m-%d %H:%M:%S")

    if now > expires_at:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return None, (jsonify({"error": "Session expired, please log in again"}), 401)

    if now - last_active_at > timedelta(minutes=get_session_idle_minutes(conn)):
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return None, (jsonify({"error": "Session timed out due to inactivity"}), 401)

    staff = conn.execute(
        "SELECT * FROM staff WHERE id = ?", (session["staff_id"],)
    ).fetchone()

    if staff is None or not staff["is_active"]:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return None, (jsonify({"error": "This staff account is not active"}), 403)

    conn.execute(
        "UPDATE sessions SET last_active_at = datetime('now', 'localtime') WHERE token = ?",
        (token,),
    )
    conn.commit()

    return staff, None


def cleanup_expired_sessions():
    """Deletes expired/idle session rows so an abandoned session doesn't sit forever."""
    conn = get_db_connection()
    try:
        idle_minutes = get_session_idle_minutes(conn)
        conn.execute(
            """
            DELETE FROM sessions
            WHERE expires_at < datetime('now', 'localtime')
            OR last_active_at < datetime('now', 'localtime', ?)
            """,
            (f"-{idle_minutes} minutes",),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"Failed to clean up expired sessions: {e}")
    finally:
        conn.close()


def login_required(f):
    """Authenticates the request via session cookie and sets g.staff.
    Use on any route that needs to know *who* is calling, without
    restricting *which* role."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        conn = get_db_connection()
        staff, error = require_session(conn)
        conn.close()
        if error:
            return _auth_failure_response(error)
        g.staff = staff
        return f(*args, **kwargs)

    return wrapper


def _auth_failure_response(error):
    """Every full-page route (routes/pages.py) shares this same auth check
    with every JSON API endpoint (everything else, all mounted under
    /api/v1) - login_required/roles_required don't know which kind of
    route they're guarding. A JS fetch()/htmx call needs the raw JSON
    error back so it can show the session-timeout modal (static/js/ui.js);
    a full page load hitting the exact same check (session cookie expired,
    then the browser was pointed straight at /transactions, /assets, etc.)
    should never render that JSON as a page - send it to /login instead,
    with `next` so it lands back where it was headed after signing in
    again. Keyed on the request path rather than headers: every non-page
    blueprint is registered under /api/v1 (app.py), so this is exact,
    not a best-effort Accept-header sniff."""
    if not request.path.startswith("/api/"):
        return redirect(f"/login?next={quote(request.full_path.rstrip('?'), safe='/?=&')}")
    return error


def feature_required(name, redirect_to_dashboard=True):
    """Gates a route behind an admin-controlled feature flag.
    redirect_to_dashboard=True toasts via a redirect param; False returns a plain JSON 403 instead."""

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not is_feature_enabled(name):
                label = get_feature_label(name)
                if redirect_to_dashboard:
                    return redirect(f"/?feature_disabled={quote(label)}")
                return jsonify({"error": f"{label} is currently disabled"}), 403
            return f(*args, **kwargs)

        return wrapper

    return decorator


def roles_required(*roles):
    """Authenticates via session cookie AND restricts to the given roles.
    Usage: @roles_required("admin", "in_charge")"""

    def decorator(f):
        @login_required
        @wraps(f)
        def wrapper(*args, **kwargs):
            if g.staff["role"] not in roles:
                return (
                    jsonify(
                        {
                            "error": f"This action requires one of these roles: {', '.join(roles)}"
                        }
                    ),
                    403,
                )
            return f(*args, **kwargs)

        return wrapper

    return decorator
