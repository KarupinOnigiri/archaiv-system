"""Thin HTTP layer para sa /setup/* endpoints - first-admin wizard ug the
temp-admin recovery path. Business logic naa sa services/setup_service.py
ug services/seed_service.py."""

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
    """GET - Public, walay auth, check kung i-show ba ang setup screen; dili
    gyud mo-return ug credentials. fresh_install mo-tell sa login page kung
    i-redirect ba diretso sa /setup (walay temp admin) or i-show ang
    temp-admin seed-notice modal (live system recovering from losing last
    real admin - see services/seed_service.py)."""
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
    """GET - Public, walay auth - para i-show sa login page ang temp admin's
    one-time credentials as modal. Naay laman ra ni kung recovery path sa
    live system; fresh install walay temp admin gyud (see
    seed_service.seed_initial_admin)."""
    notice = get_seed_notice()
    return jsonify(notice or {})


@setup_bp.route("/setup/complete", methods=["POST"])
@limiter.limit("10 per minute")
def complete_setup():
    """POST - walay @login_required kay ang true fresh install (walay staff
    pa) walay ma-login as, so mag-create dayon ni ug first admin account.
    Sa uban case (live system nga re-seeded ug temp admin after nawad-an sa
    last real admin) required gihapon login as temp admin, checked manually
    diri via require_session para ma-distinguish ang duha ka path."""
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
