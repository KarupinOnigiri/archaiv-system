"""Advance booking of a specific asset for a member, ahead of time - see
services/reservation_service.py's module docstring for the full design
(especially why this never touches assets.status, and the v1 advisory-only
scope decision).
"""

from flask import Blueprint, jsonify, g, request, render_template
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required, roles_required
from services.settings_service import is_feature_enabled, get_feature_label
from services import reservation_service
from utils.validators import parse_pagination_params, validate_date_string
from extensions import limiter

reservations_bp = Blueprint("reservations", __name__)


@reservations_bp.before_request
def _block_if_reservations_disabled():
    if not is_feature_enabled("reservations"):
        return jsonify({"error": f"{get_feature_label('reservations')} is currently disabled"}), 403


VALID_STATUSES = reservation_service.VALID_STATUSES


@reservations_bp.route("/assets/<int:id>/reserve-form")
@login_required
def reserve_form(id):
    conn = get_db_connection()
    try:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
    finally:
        conn.close()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    if asset["asset_type"] == "consumable":
        return jsonify({"error": "Consumables can't be reserved"}), 400
    return render_template("reservations/_reserve_modal.html", asset=asset, error=None)


@reservations_bp.route("/reservations", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def create_reservation():
    data, error = get_json_or_error()
    if error:
        return error

    asset_id = data.get("asset_id")
    member_id = data.get("member_id")
    start_at = data.get("start_at")
    end_at = data.get("end_at")
    notes = data.get("notes")

    for field, value in (("asset_id", asset_id), ("member_id", member_id), ("start_at", start_at), ("end_at", end_at)):
        if not value:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    for field, value in (("start_at", start_at), ("end_at", end_at)):
        date_error = validate_date_string(value, field)
        if date_error:
            return date_error

    conn = get_db_connection()
    try:
        result, error = reservation_service.create_reservation(
            conn, asset_id, member_id, start_at, end_at, g.staff, notes
        )
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 201


@reservations_bp.route("/reservations/<int:id>/cancel", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def cancel_reservation(id):
    conn = get_db_connection()
    try:
        result, error = reservation_service.cancel_reservation(conn, id, g.staff)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@reservations_bp.route("/reservations")
@login_required
def list_reservations():
    """admin/in_charge see every reservation; tool_keeper only sees their own."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        context, error = reservation_service.get_reservations_context(conn, request.args, limit, offset, g.staff)
    finally:
        conn.close()

    if error:
        return error

    if request.headers.get("HX-Request") == "true":
        return render_template("reservations/_table.html", staff=g.staff, **context)

    return jsonify(
        {
            "total_count": context["total_count"],
            "limit": context["limit"],
            "offset": context["offset"],
            "results": [dict(row) for row in context["reservations_list"]],
        }
    )


@reservations_bp.route("/assets/<int:id>/reservations")
@login_required
def asset_reservations(id):
    """Upcoming reservations for one asset - feeds the Assets Details
    modal's "RESERVED FOR" banner."""
    conn = get_db_connection()
    try:
        upcoming = reservation_service.get_upcoming_reservations_for_asset(conn, id)
    finally:
        conn.close()
    return jsonify({"reservations": upcoming})
