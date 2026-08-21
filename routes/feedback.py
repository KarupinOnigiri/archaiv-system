from flask import Blueprint, jsonify, g, request, render_template
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required, roles_required
from services import feedback_service
from extensions import limiter

feedback_bp = Blueprint("feedback", __name__)

_CATEGORIES = feedback_service.CATEGORIES


def get_open_feedback_count(conn=None):
    """Re-exported here because app.py imports it directly from routes.feedback."""
    return feedback_service.get_open_feedback_count(conn)


# # # SUBMIT (any signed-in staff member) -----------------------------------


@feedback_bp.route("/feedback", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def submit_feedback():
    """Handles 'Give feedback'; open to every role. Multipart form, not JSON, since it takes screenshots."""
    category = request.form.get("category")
    message = (request.form.get("message") or "").strip()
    page_path = (request.form.get("page_path") or "").strip()[:200]
    # getlist because the <input> allows multiple files under one field name
    screenshots = [f for f in request.files.getlist("screenshots") if f and f.filename]

    conn = get_db_connection()
    try:
        result, error = feedback_service.submit_feedback(conn, g.staff, category, message, page_path, screenshots)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


# # # REVIEW (admin only) ---------------------------------------------------


def get_feedback_table_context(args):
    conn = get_db_connection()
    try:
        return feedback_service.get_feedback_table_context(conn, args)
    finally:
        conn.close()


@feedback_bp.route("/feedback/table")
@roles_required("admin")
def view_feedback_table():
    context, error = get_feedback_table_context(request.args)
    if error:
        return error
    return render_template("feedback/_table.html", **context)


@feedback_bp.route("/feedback/<int:id>/resolve", methods=["POST"])
@roles_required("admin")
def resolve_feedback(id):
    """Resolving requires an actual reply, not a silent status flip."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    response = (data.get("response") or "").strip()

    conn = get_db_connection()
    try:
        result, error = feedback_service.resolve_feedback(conn, id, response, g.staff)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@feedback_bp.route("/feedback/<int:id>/reopen", methods=["POST"])
@roles_required("admin")
def reopen_feedback(id):
    conn = get_db_connection()
    try:
        result, error = feedback_service.reopen_feedback(conn, id, g.staff)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)
