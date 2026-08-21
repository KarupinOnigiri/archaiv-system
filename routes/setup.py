from flask import Blueprint, jsonify, g
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required
from utils.validators import validate_password_strength
from services.seed_service import get_seed_notice
from services import setup_service
from extensions import limiter

setup_bp = Blueprint("setup", __name__)


@setup_bp.route("/setup/status")
@limiter.limit("30 per minute")
def setup_status():
    """Public, no-auth check for whether to show the setup screen; never returns credentials."""
    return jsonify({"setup_required": setup_service.is_setup_required()})


@setup_bp.route("/setup/seed-notice")
@limiter.limit("30 per minute")
def setup_seed_notice():
    """Public, no-auth -- lets the login page show the temp admin's one-time credentials as a modal."""
    notice = get_seed_notice()
    return jsonify(notice or {})


@setup_bp.route("/setup/complete", methods=["POST"])
@limiter.limit("10 per minute")
@login_required
def complete_setup():
    data, error = get_json_or_error()
    if error:
        return error

    # barcode_id isn't from the form -- it's auto-generated below, same as every other member.
    required_fields = ("first_name", "last_name", "department_id", "username", "password")
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}), 400

    error = validate_password_strength(data.get("password"))
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = setup_service.complete_setup(conn, data, g.staff)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 201
