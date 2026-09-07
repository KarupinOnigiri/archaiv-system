from flask import Blueprint, jsonify, g, request, render_template, redirect
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required
from services import notification_service

notifications_bp = Blueprint("notifications", __name__)


def get_notifications_table_context(args, staff):
    conn = get_db_connection()
    try:
        return notification_service.get_notifications_table_context(conn, args, staff)
    finally:
        conn.close()


@notifications_bp.route("/notifications/table")
@login_required
def view_notifications_table():
    """htmx partial - just the list + pagination footer, so paging doesn't re-render the whole page."""
    context, error = get_notifications_table_context(request.args, g.staff)
    if error:
        return error
    return render_template("notifications/_table.html", **context)


@notifications_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    conn = get_db_connection()
    try:
        notification_service.mark_all_notifications_read(conn, g.staff)
    finally:
        conn.close()
    return jsonify({"message": "All notifications marked as read"})


@notifications_bp.route("/notifications/<int:id>/mark-read", methods=["POST"])
@login_required
def mark_notification_read(id):
    conn = get_db_connection()
    try:
        found = notification_service.mark_notification_read(conn, id, g.staff)
    finally:
        conn.close()
    if not found:
        return jsonify({"error": "Notification not found"}), 404
    return jsonify({"message": "Notification marked as read"})


# # # CLEARING ---------------------------------------------------------------
# Clearing is the only way a notification leaves the list; per staff member, not shared.


@notifications_bp.route("/notifications/<int:id>/clear", methods=["POST"])
@login_required
def clear_notification(id):
    """The '×' on each row - clears one notification at a time."""
    conn = get_db_connection()
    try:
        found = notification_service.clear_notification(conn, id, g.staff)
    finally:
        conn.close()
    if not found:
        return jsonify({"error": "Notification not found"}), 404
    return jsonify({"message": "Notification cleared"})


@notifications_bp.route("/notifications/clear", methods=["POST"])
@login_required
def clear_notifications():
    """"Clear selected" bulk action. Body: {"ids": [1, 2, 3]}."""
    data, error = get_json_or_error()
    if error:
        return error

    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids must be a non-empty list"}), 400
    if not all(isinstance(i, int) for i in ids):
        return jsonify({"error": "ids must be a list of integers"}), 400

    conn = get_db_connection()
    try:
        cleared_count = notification_service.clear_notifications_bulk(conn, ids, g.staff)
    finally:
        conn.close()

    return jsonify({"message": f"{cleared_count} notification(s) cleared", "cleared_count": cleared_count})


@notifications_bp.route("/notifications/<int:id>/open")
@login_required
def open_notification(id):
    """Marks a notification read, then redirects by type: loans or requests.
    Plain GET despite the side effect, like an email client's click-to-open-and-clear pattern."""
    conn = get_db_connection()
    try:
        target = notification_service.resolve_notification_open(conn, id, g.staff)
    finally:
        conn.close()

    return redirect(target or "/notifications")
