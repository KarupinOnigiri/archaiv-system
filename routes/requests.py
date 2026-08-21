from flask import Blueprint, jsonify, g, request, render_template
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from services.settings_service import is_feature_enabled, get_feature_label
from services import unrestriction_service
from utils.validators import parse_pagination_params
from extensions import limiter

requests_bp = Blueprint("requests", __name__)


@requests_bp.before_request
def _block_if_requests_disabled():
    if not is_feature_enabled("requests"):
        return jsonify({"error": f"{get_feature_label('requests')} is currently disabled"}), 403


VALID_STATUSES = unrestriction_service.VALID_STATUSES


# # # CREATE (tool_keeper flags a restricted member for review) -----------


@requests_bp.route("/unrestriction-requests", methods=["POST"])
@limiter.limit("20 per minute")
@roles_required("tool_keeper")
def create_unrestriction_request():
    data, error = get_json_or_error()
    if error:
        return error

    member_id = data.get("member_id")
    reason = data.get("reason")

    if not member_id:
        return jsonify({"error": "Missing required field: member_id"}), 400
    if not reason:
        return jsonify({"error": "Missing required field: reason"}), 400

    conn = get_db_connection()
    try:
        result, error = unrestriction_service.create_unrestriction_request(conn, member_id, reason, g.staff)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 201


@requests_bp.route("/unrestriction-requests/<int:id>/cancel", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def cancel_unrestriction_request(id):
    """Lets the requester withdraw their own pending request (unlike /deny, which is admin/in_charge only)."""
    data, error = get_json_or_error()
    review_notes = data.get("review_notes") if not error else None

    conn = get_db_connection()
    try:
        result, error = unrestriction_service.cancel_unrestriction_request(conn, id, review_notes, g.staff)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


# # # LIST (the "tab") ----------------------------------------------------


@requests_bp.route("/unrestriction-requests")
@login_required
def list_unrestriction_requests():
    """admin/in_charge see the full review queue; tool_keeper only sees their own requests."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        context, error = unrestriction_service.get_unrestriction_requests_context(
            conn, request.args, limit, offset, g.staff
        )
    finally:
        conn.close()

    if error:
        return error

    if request.headers.get("HX-Request") == "true":
        # staff=g.staff needed here too, or the partial's can_review check silently breaks after htmx swaps.
        return render_template("requests/_unrestriction_table.html", staff=g.staff, **context)

    return jsonify(
        {
            "total_count": context["total_count"],
            "limit": context["limit"],
            "offset": context["offset"],
            "results": [dict(row) for row in context["requests_list"]],
        }
    )


# # # REVIEW (admin/in_charge only) ---------------------------------------


@requests_bp.route("/unrestriction-requests/<int:id>/approve", methods=["POST"])
@limiter.limit("20 per minute")
@roles_required("admin", "in_charge")
def approve_unrestriction_request(id):
    data, error = get_json_or_error()
    review_notes = data.get("review_notes") if not error else None

    conn = get_db_connection()
    try:
        result, error = unrestriction_service.approve_unrestriction_request(conn, id, review_notes, g.staff)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@requests_bp.route("/unrestriction-requests/<int:id>/deny", methods=["POST"])
@limiter.limit("20 per minute")
@roles_required("admin", "in_charge")
def deny_unrestriction_request(id):
    data, error = get_json_or_error()
    if error:
        return error

    review_notes = data.get("review_notes")
    if not review_notes:
        return jsonify({"error": "Missing required field: review_notes"}), 400

    conn = get_db_connection()
    try:
        result, error = unrestriction_service.deny_unrestriction_request(conn, id, review_notes, g.staff)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)
