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
    """The core auth check every request ultimately goes through - reads the
    session_token cookie, validates it (exists, not expired, not idle-timed-
    out, CSRF token matches on state-changing methods, owning staff account
    still active), and refreshes last_active_at on success. login_required/
    roles_required below just wrap this as a decorator; a couple of routes
    (e.g. the "/" dashboard) call it directly instead.
    (Bisaya: Kini ang pinaka-importante nga auth check - dinhi mo-agi ang
    tanang request. Mo-basa sa session_token cookie, susihon kung
    (1) naa ba gyud, (2) wala pa ba na-expire, (3) wala pa ba na-idle-timeout,
    (4) tukma ba ang CSRF token - para lang sa mga POST/PUT/PATCH/DELETE -
    ug (5) buhi/active pa ba ang staff account nga tag-iya sa session. Kung
    maayo ang tanan, i-update ang last_active_at. Ang login_required ug
    roles_required sa ubos ni-wrap ra ani isip decorator; pipila ka route -
    e.g. ang "/" dashboard - mutawag direkta ani.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)

    Returns:
        tuple (staff, error) - exactly one is not None:
        - staff: the sqlite3.Row for the logged-in staff member, on success.
        - error: a ready-to-return (jsonify({"error": ...}), status) tuple
          explaining why the session was rejected (401/403), on failure.
        (Bisaya: usa ka tuple (staff, error) - usa lang sa duha ang naay
        laman: ang `staff` naa'y laman kung malampuson - kini ang row sa
        naka-login nga staff; ang `error` naa'y laman - andam na i-return -
        kung gi-reject ang session, e.g. 401 o 403.)
    """
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
    """Deletes expired/idle session rows so an abandoned session doesn't sit forever.
    (Bisaya: Mag-tangtang sa mga session row nga na-expire na o na-idle na -
    para dili magpabilin ang mga session nga gibiyaan na sa tawo. Gitawag ni
    sa background scheduler, walay parameter ug walay return value.)"""
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
    restricting *which* role.
    (Bisaya: Decorator nga mag-check sa session cookie ug mag-set sa
    g.staff. Gamiton sa bisan unsa nga route nga kinahanglan mahibaw-an
    KINSA ang nag-request, pero wala nagrestrict kung UNSA nga role ra ang
    pwede.)

    Args:
        f: the Flask view function being decorated.
        (Bisaya: ang view function nga gi-decorate.)

    Returns:
        the wrapped view function - calls f(*args, **kwargs) with
        g.staff already set when the session is valid; otherwise returns
        _auth_failure_response()'s result without ever calling f.
        (Bisaya: ang na-wrap nga view function - tawagon ang f(*args,
        **kwargs) nga naa nay g.staff kung balido ang session; kung dili,
        ang _auth_failure_response() ra ang i-return, dili na tawagon ang
        f.)
    """

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
    not a best-effort Accept-header sniff.
    (Bisaya: Kini gigamit sa TANANG route - full-page (routes/pages.py) ug
    JSON API (ang uban, tanan naa sa ilawom sa /api/v1) - pareho ra ang
    auth check nga ilang gigamit. Ang JS fetch()/htmx nga request
    kinahanglan mubalik ug tinuod nga JSON error para makita ang
    session-timeout modal (static/js/ui.js); apan ang full page load - ang
    browser gitudlo dayon sa /transactions, /assets, ug uban pa - dili
    angay mopakita anang JSON isip page, kinahanglan ipadala sa /login
    hinuon, dala ang `next` para mobalik sa gikan human makalogin pag-usab.
    Gibase sa path sa request, dili sa headers - kay ang matag
    non-page blueprint naa gyud sa ilawom sa /api/v1 (app.py), busa sigurado
    ni, dili lang "guess" gikan sa Accept header.)

    Args:
        error: the (jsonify({"error": ...}), status) tuple from
            require_session()'s failed branch.
        (Bisaya: ang (jsonify({"error": ...}), status) nga tuple gikan sa
        napakyas nga check sa require_session().)

    Returns:
        a Flask response - either `error` as-is (for /api/ requests), or a
        redirect to /login?next=... (for a full-page request).
        (Bisaya: usa ka Flask response - ang `error` mismo kung /api/
        request, o redirect ngadto sa /login?next=... kung full-page
        request.)
    """
    if not request.path.startswith("/api/"):
        return redirect(f"/login?next={quote(request.full_path.rstrip('?'), safe='/?=&')}")
    return error


def feature_required(name, redirect_to_dashboard=True):
    """Gates a route behind an admin-controlled feature flag.
    redirect_to_dashboard=True toasts via a redirect param; False returns a plain JSON 403 instead.
    (Bisaya: Mag-block sa usa ka route kung gi-disable kini nga feature sa
    admin - System > Feature Access. Kung redirect_to_dashboard=True,
    mo-redirect sa dashboard nga naay "toast" nga mensahe pinaagi sa
    query param; kung False, plain JSON 403 ra ang ibalik - gigamit para sa
    mga API endpoint nga wala nay sunod nga page nga i-redirect-an.)

    Args:
        name: the feature flag's key, e.g. "assets", "reservations" - must
            match what settings_service.py's is_feature_enabled() expects.
        (Bisaya: ang key/ngalan sa feature flag, e.g. "assets",
        "reservations" - kinahanglan pareho sa gina-expect sa
        settings_service.py's is_feature_enabled().)
        redirect_to_dashboard: True (default) for full-page routes - toasts
            via a redirect; False for JSON API routes - plain 403 instead.
        (Bisaya: True (default) para sa full-page nga route - mo-redirect
        nga naay toast; False para sa JSON API nga route - plain 403 ra.)

    Returns:
        the decorator function to apply to a Flask view.
        (Bisaya: ang decorator function nga i-apply sa Flask view.)
    """

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
    Usage: @roles_required("admin", "in_charge")
    (Bisaya: Mag-check sa session cookie UG mag-restrict lang sa mga role
    nga gi-pasa. Gamiton pananglitan: @roles_required("admin",
    "in_charge"). Naay built-in nga @login_required na sa sulod, dili na
    kinahanglan i-stack pa ang duha - kining usa ra igo na.)

    Args:
        *roles: one or more role strings ("admin", "in_charge",
            "tool_keeper") - the request is allowed through only if
            g.staff["role"] is one of these.
        (Bisaya: usa o daghan nga role - "admin", "in_charge",
        "tool_keeper" - tugotan lang ang request kung ang g.staff["role"]
        usa niini.)

    Returns:
        the decorator function to apply to a Flask view.
        (Bisaya: ang decorator function nga i-apply sa Flask view.)
    """

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
