from flask import Blueprint, jsonify, request, render_template
from database import get_db_connection
from services.auth_service import login_required
from services.dashboard_service import (
    get_top_borrowed,
    TOP_BORROWED_RANGES,
    TOP_BORROWED_RANGE_LABELS,
)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard/top-borrowed")
@login_required
def view_top_borrowed():
    """htmx partial for the Top Borrowed filter, using the same query as page load."""
    range_key = request.args.get("range", "30d")
    if range_key not in TOP_BORROWED_RANGES:
        return jsonify({"error": f"range must be one of: {', '.join(TOP_BORROWED_RANGES)}"}), 400

    conn = get_db_connection()
    dash_top_borrowed = get_top_borrowed(conn, range_key)
    conn.close()

    return render_template(
        "dashboard/_top_borrowed_list.html",
        dash_top_borrowed=dash_top_borrowed,
        range_label=TOP_BORROWED_RANGE_LABELS[range_key],
    )
