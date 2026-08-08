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

    # CSRF check — only required for state-changing methods
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
    """Deletes session rows that are expired or have gone past the idle
    timeout, even if nobody ever tried to use that token again. Sessions
    are also lazily deleted the moment someone attempts to use a stale
    token (see require_session above), but an abandoned session — browser
    closed, kiosk terminal power-cycled mid-shift — would otherwise sit in
    the table indefinitely. Run on a schedule so it doesn't grow unbounded."""
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


def validate_staff(staff_id, conn):
    """Returns (staff_row, error_response) — staff_row is None if invalid."""
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

    if staff is None:
        return None, (
            jsonify({"error": "Invalid staff_id: staff member not found"}),
            404,
        )

    if not staff["is_active"]:
        return None, (jsonify({"error": "This staff account is not active"}), 403)

    return staff, None


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
            return error
        g.staff = staff
        return f(*args, **kwargs)

    return wrapper


def feature_required(name, redirect_to_dashboard=True):
    """Gates a route behind an admin-controlled feature flag (see
    services/settings_service.py's FEATURE_FLAGS, migrations/0033_feature_flags.sql).

    redirect_to_dashboard=True (the default, for the *_page view functions
    in routes/pages.py) sends a disabled-tab visit back to '/' with a
    ?feature_disabled=<label> query param base.html's JS picks up and
    toasts -- this app has no server-side flash(), so a query param is the
    established way to hand a one-shot message across a redirect.

    redirect_to_dashboard=False (for use in a blueprint's before_request,
    see routes/assets.py etc.) returns a plain JSON 403 instead, matching
    every other error response those API blueprints already give.

    Stacks under @roles_required/@login_required -- put this decorator
    closest to the view function so the role check still runs first."""

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
