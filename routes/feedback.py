from flask import Blueprint, jsonify, g, request, render_template
from database import get_db_connection
from services.auth_service import login_required, roles_required
from utils.logger import log_action
from utils.formatters import full_name
from utils.uploads import (
    save_feedback_screenshot,
    delete_feedback_screenshot,
    MAX_FEEDBACK_IMAGES,
)
from utils.validators import parse_pagination_params
from extensions import limiter

feedback_bp = Blueprint("feedback", __name__)

_CATEGORIES = ("bug", "idea", "other")


# # # SUBMIT (any signed-in staff member) -----------------------------------


@feedback_bp.route("/feedback", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def submit_feedback():
    """The 'Give feedback' row in the account dropdown (templates/base.html)
    -- an in-app bug/issue report, reviewed by admins from the Feedback
    tab (view_feedback_page / view_feedback_table below). Open to every
    role, not just admin/in_charge: a tool_keeper hitting a bug is exactly
    who needs to be able to report it.

    multipart/form-data rather than JSON -- the JSON get_json_or_error
    helper used by the rest of this app can't carry file uploads, and this
    endpoint accepts up to MAX_FEEDBACK_IMAGES screenshots alongside the
    category/message fields."""
    category = request.form.get("category")
    message = (request.form.get("message") or "").strip()
    page_path = (request.form.get("page_path") or "").strip()[:200]
    # getlist, not just request.files.get -- the <input> is `multiple`, so
    # a submission can carry more than one screenshot under the same field
    # name.
    screenshots = [
        f for f in request.files.getlist("screenshots") if f and f.filename
    ]

    if category not in _CATEGORIES:
        return jsonify({"error": f"category must be one of {', '.join(_CATEGORIES)}"}), 400
    if not message:
        return jsonify({"error": "message is required"}), 400
    if len(message) > 4000:
        return jsonify({"error": "message must be 4000 characters or fewer"}), 400
    if len(screenshots) > MAX_FEEDBACK_IMAGES:
        return jsonify({"error": f"Attach at most {MAX_FEEDBACK_IMAGES} screenshots"}), 400

    conn = get_db_connection()
    saved_paths = []
    try:
        cursor = conn.execute(
            """
            INSERT INTO feedback_reports (staff_id, category, message, page_path)
            VALUES (?, ?, ?, ?)
            """,
            (g.staff["id"], category, message, page_path or None),
        )
        report_id = cursor.lastrowid

        for file_storage in screenshots:
            relative_path, error = save_feedback_screenshot(file_storage, report_id)
            if error:
                # Roll back the DB insert and delete anything already
                # saved to disk this request, rather than leaving a
                # feedback report with only some of its screenshots
                # attached because the third file happened to be corrupt.
                conn.rollback()
                for p in saved_paths:
                    delete_feedback_screenshot(p)
                return jsonify({"error": f"One screenshot couldn't be saved: {error}"}), 400
            saved_paths.append(relative_path)
            conn.execute(
                "INSERT INTO feedback_report_images (feedback_report_id, image_path) VALUES (?, ?)",
                (report_id, relative_path),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        for p in saved_paths:
            delete_feedback_screenshot(p)
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="submitted_feedback",
        target_table="feedback_reports",
        target_id=report_id,
        details=f"category={category}, screenshots={len(saved_paths)}",
    )

    return jsonify({"message": "Thanks -- your feedback was sent to the admins."})


# # # REVIEW (admin only) ---------------------------------------------------


def get_open_feedback_count(conn=None):
    """Feeds the System/Feedback sidebar links' badges (templates/base.html),
    same pattern as pending_approvals_count / unread_notifications_count."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS cnt FROM feedback_reports WHERE status = 'open'"
        ).fetchone()["cnt"]
    finally:
        if owns_conn:
            conn.close()


def get_feedback_table_context(args):
    """Shared query logic for the Feedback tab -- same get_*_table_context
    pattern used by assets/members/staff/transactions/notifications.
    Returns (context_dict, error)."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    status = args.get("status", "open")
    if status not in ("open", "resolved", "all"):
        return None, (jsonify({"error": "status must be one of: open, resolved, all"}), 400)

    category = args.get("category") or None
    if category is not None and category not in _CATEGORIES:
        return None, (jsonify({"error": f"category must be one of {', '.join(_CATEGORIES)}"}), 400)

    where_clauses = []
    params = []
    if status != "all":
        where_clauses.append("feedback_reports.status = ?")
        params.append(status)
    if category:
        where_clauses.append("feedback_reports.category = ?")
        params.append(category)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    conn = get_db_connection()
    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM feedback_reports
        JOIN staff ON staff.id = feedback_reports.staff_id
        {where_sql}
        """,
        params,
    ).fetchone()["cnt"]
    reports = conn.execute(
        f"""
        SELECT feedback_reports.*,
               members.first_name, members.last_name, staff.username
        FROM feedback_reports
        JOIN staff ON staff.id = feedback_reports.staff_id
        JOIN members ON members.id = staff.member_id
        {where_sql}
        ORDER BY feedback_reports.created_at DESC, feedback_reports.id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    # One extra query for every image belonging to a report on this page,
    # rather than N+1 queries (one per report) -- grouped into a dict here
    # so the template can just do images_by_report.get(r.id, []).
    report_ids = [r["id"] for r in reports]
    images_by_report = {rid: [] for rid in report_ids}
    if report_ids:
        placeholders = ",".join("?" * len(report_ids))
        image_rows = conn.execute(
            f"""
            SELECT * FROM feedback_report_images
            WHERE feedback_report_id IN ({placeholders})
            ORDER BY id ASC
            """,
            report_ids,
        ).fetchall()
        for img in image_rows:
            images_by_report[img["feedback_report_id"]].append(img)

    open_count = get_open_feedback_count(conn)
    conn.close()

    return {
        "reports": reports,
        "images_by_report": images_by_report,
        "open_count": open_count,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "status": status,
        "category": category or "",
        "full_name": full_name,
    }, None


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
    conn = get_db_connection()
    report = conn.execute(
        "SELECT * FROM feedback_reports WHERE id = ?", (id,)
    ).fetchone()
    if report is None:
        conn.close()
        return jsonify({"error": "Feedback report not found"}), 404

    conn.execute(
        """
        UPDATE feedback_reports
        SET status = 'resolved', resolved_at = datetime('now', 'localtime'), resolved_by_staff_id = ?
        WHERE id = ?
        """,
        (g.staff["id"], id),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="resolved_feedback",
        target_table="feedback_reports",
        target_id=id,
    )

    return jsonify({"message": "Marked resolved"})


@feedback_bp.route("/feedback/<int:id>/reopen", methods=["POST"])
@roles_required("admin")
def reopen_feedback(id):
    conn = get_db_connection()
    report = conn.execute(
        "SELECT * FROM feedback_reports WHERE id = ?", (id,)
    ).fetchone()
    if report is None:
        conn.close()
        return jsonify({"error": "Feedback report not found"}), 404

    conn.execute(
        """
        UPDATE feedback_reports
        SET status = 'open', resolved_at = NULL, resolved_by_staff_id = NULL
        WHERE id = ?
        """,
        (id,),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="reopened_feedback",
        target_table="feedback_reports",
        target_id=id,
    )

    return jsonify({"message": "Reopened"})
