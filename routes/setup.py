from flask import Blueprint, jsonify
from database import get_db_connection, get_json_or_error
from services.auth_service import require_session
from utils.validators import validate_password_strength
from services.seed_service import get_seed_notice
from services import setup_service
from extensions import limiter

setup_bp = Blueprint("setup", __name__)


@setup_bp.route("/setup/status")
@limiter.limit("30 per minute")
def setup_status():
    """Public, no-auth check for whether to show the setup screen; never returns credentials.
    fresh_install additionally tells the login page whether to send the visitor straight to
    /setup (no temp admin exists to log in as) or show the temp-admin seed-notice modal instead
    (a live system recovering from losing its last real admin - see services/seed_service.py)."""
    conn = get_db_connection()
    try:
        return jsonify(
            {
                "setup_required": setup_service.is_setup_required(conn),
                "fresh_install": setup_service.is_fresh_install(conn),
            }
        )
    finally:
        conn.close()


@setup_bp.route("/setup/seed-notice")
@limiter.limit("30 per minute")
def setup_seed_notice():
    """Public, no-auth - lets the login page show the temp admin's one-time credentials as a
    modal. Only ever returns something on a live system's recovery path; a fresh install never
    has a temp admin to begin with (see seed_service.seed_initial_admin)."""
    notice = get_seed_notice()
    return jsonify(notice or {})


@setup_bp.route("/setup/complete", methods=["POST"])
@limiter.limit("10 per minute")
def complete_setup():
    """No @login_required here on purpose: a true fresh install (no staff at all yet) has no
    one to log in as, so this creates the first admin account directly. Anything else - a live
    system that re-seeded a temp admin after losing its last real admin - still requires being
    logged in as that temp admin, checked manually below via require_session so the two paths
    can be told apart before deciding whether a session is even required."""
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = ("first_name", "last_name", "department_id", "username", "password")
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}), 400

    error = validate_password_strength(data.get("password"))
    if error:
        return error

    conn = get_db_connection()
    try:
        if setup_service.is_fresh_install(conn):
            temp_admin = None
        else:
            temp_admin, session_error = require_session(conn)
            if session_error:
                return session_error

        result, error = setup_service.complete_setup(conn, data, temp_admin)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 201
