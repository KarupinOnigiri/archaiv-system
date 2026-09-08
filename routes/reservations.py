"""Advance booking sa usa ka specific asset para sa usa ka member - see
services/reservation_service.py's module docstring sa full design
(especially ngano wala ni ga-touch sa assets.status, ug ang v1
advisory-only scope decision)."""

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
    """Mo-403 sa tanan /reservations/* endpoints kung naka-disable ang
    reservations feature flag."""
    if not is_feature_enabled("reservations"):
        return jsonify({"error": f"{get_feature_label('reservations')} is currently disabled"}), 403


VALID_STATUSES = reservation_service.VALID_STATUSES


@reservations_bp.route("/reservations/new-form")
@login_required
def new_reservation_form():
    """GET - modal shell para sa Reservations page's "New Reservation"
    button - asset picker (search/browse) una, dayon mo-swap ngadto sa
    reserve_form's own modal human mapili ang asset."""
    return render_template("reservations/_new_reservation_modal.html")


@reservations_bp.route("/reservations/browse-assets")
@login_required
def browse_reservable_assets():
    """GET - JSON list sa reservable assets (non-consumable, dili pa
    deleted) para sa "New Reservation" picker's search/browse."""
    q = request.args.get("q", "")
    conn = get_db_connection()
    try:
        assets = reservation_service.search_reservable_assets(conn, q)
    finally:
        conn.close()
    return jsonify({"assets": assets})


@reservations_bp.route("/assets/<int:id>/reserve-form")
@login_required
def reserve_form(id):
    """GET - modal form para mag-reserve ug asset gikan sa Details modal's
    "Reserve for Later" button. Consumables dili pwede i-reserve."""
    conn = get_db_connection()
    try:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
    finally:
        conn.close()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    if asset["asset_type"] == "consumable":
        return jsonify({"error": "Consumables can't be reserved"}), 400
    if asset["status"] == "maintenance":
        return jsonify({"error": "This asset is in maintenance and can't be reserved right now"}), 400
    return render_template("reservations/_reserve_modal.html", asset=asset, error=None)


@reservations_bp.route("/reservations", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def create_reservation():
    """POST - mag-create ug bag-ong reservation. Delegates to
    reservation_service.create_reservation() after validating required
    fields ug date strings."""
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
    """POST - i-cancel ang reservation. Delegates to
    reservation_service.cancel_reservation()."""
    conn = get_db_connection()
    try:
        result, error = reservation_service.cancel_reservation(conn, id, g.staff)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@reservations_bp.route("/reservations/<int:id>/details-modal")
@login_required
def reservation_details_modal(id):
    """GET - row-click modal sa Reservations table showing full detail sa
    usa ka reservation. Delegates to reservation_service.get_reservation_details()."""
    conn = get_db_connection()
    try:
        reservation, error = reservation_service.get_reservation_details(conn, id, g.staff)
    finally:
        conn.close()
    if error:
        return error
    return render_template("reservations/_details_modal.html", r=reservation, staff=g.staff)


@reservations_bp.route("/reservations")
@login_required
def list_reservations():
    """GET - admin/in_charge makakita sa tanan reservations; tool_keeper
    makakita ra sa iyang own. HX-Request header mo-decide kung partial
    template or plain JSON ang i-return."""
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
    """GET - upcoming reservations para sa usa ka asset - feeds ang Assets
    Details modal's "RESERVED FOR" banner."""
    conn = get_db_connection()
    try:
        upcoming = reservation_service.get_upcoming_reservations_for_asset(conn, id)
    finally:
        conn.close()
    return jsonify({"reservations": upcoming})
