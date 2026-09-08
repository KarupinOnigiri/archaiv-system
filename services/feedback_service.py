"""Query-building and mutation logic for routes/feedback.py (in-app bug/idea/other
reports staff can submit, admins can review and resolve).

Same (data, error) convention sa laing *_service modules - kini nga file
ang naghandle sa feedback (bug/idea/other) nga pwede i-submit sa staff
sulod mismo sa app, mao pod ang review/resolve sa admin.
"""

from flask import jsonify

from database import get_db_connection
from utils.formatters import full_name
from utils.logger import log_action
from utils.uploads import save_feedback_screenshot, delete_feedback_screenshot, MAX_FEEDBACK_IMAGES
from utils.validators import parse_pagination_params
from services.notification_service import (
    create_feedback_submitted_notifications,
    create_feedback_resolved_notification,
)

CATEGORIES = ("bug", "idea", "other")


def submit_feedback(conn, staff, category, message, page_path, screenshots):
    """Mo-himo ug feedback report nga naay 0+ ka screenshot, tanan-o-wala -
    kung mapakyas ang usa ka screenshot, mo-rollback ang TIBUOK report,
    dili tugotan nga partial/kulang ra ang nasave.

    Returns: tuple (result, error)."""
    if category not in CATEGORIES:
        return None, (jsonify({"error": f"category must be one of {', '.join(CATEGORIES)}"}), 400)
    if not message:
        return None, (jsonify({"error": "message is required"}), 400)
    if len(message) > 4000:
        return None, (jsonify({"error": "message must be 4000 characters or fewer"}), 400)
    if len(screenshots) > MAX_FEEDBACK_IMAGES:
        return None, (jsonify({"error": f"Attach at most {MAX_FEEDBACK_IMAGES} screenshots"}), 400)

    saved_paths = []
    try:
        cursor = conn.execute(
            "INSERT INTO feedback_reports (staff_id, category, message, page_path) VALUES (?, ?, ?, ?)",
            (staff["id"], category, message, page_path or None),
        )
        report_id = cursor.lastrowid

        for file_storage in screenshots:
            relative_path, error = save_feedback_screenshot(file_storage, report_id)
            if error:
                # bail out fully rather than leave a report with only some screenshots attached
                conn.rollback()
                for p in saved_paths:
                    delete_feedback_screenshot(p)
                return None, (jsonify({"error": f"One screenshot couldn't be saved: {error}"}), 400)
            saved_paths.append(relative_path)
            conn.execute(
                "INSERT INTO feedback_report_images (feedback_report_id, image_path) VALUES (?, ?)",
                (report_id, relative_path),
            )

        # Same transaction as the insert; g.staff has no joined member row, only username here.
        create_feedback_submitted_notifications(conn, report_id, staff["id"], staff["username"], category)

        conn.commit()
    except Exception:
        conn.rollback()
        for p in saved_paths:
            delete_feedback_screenshot(p)
        raise

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="submitted_feedback",
        target_table="feedback_reports",
        target_id=report_id,
        details=f"category={category}, screenshots={len(saved_paths)}",
    )

    return {"message": "Thanks - your feedback was sent to the admins."}, None


# # # REVIEW (admin only) ---------------------------------------------------


def get_open_feedback_count(conn=None):
    """Gigamit sa System/Feedback sidebar badge, pareho ka porma sa laing
    pending-count nga helper. Returns: int."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS cnt FROM feedback_reports WHERE status = 'open'").fetchone()["cnt"]
    finally:
        if owns_conn:
            conn.close()


def get_feedback_table_context(conn, args):
    """Shared query para sa Feedback tab, pareho ka porma sa
    get_*_table_context sa laing tabs.

    Returns: tuple (context, error)."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    status = args.get("status", "open")
    if status not in ("open", "resolved", "all"):
        return None, (jsonify({"error": "status must be one of: open, resolved, all"}), 400)

    category = args.get("category") or None
    if category is not None and category not in CATEGORIES:
        return None, (jsonify({"error": f"category must be one of {', '.join(CATEGORIES)}"}), 400)

    where_clauses = []
    params = []
    if status != "all":
        where_clauses.append("feedback_reports.status = ?")
        params.append(status)
    if category:
        where_clauses.append("feedback_reports.category = ?")
        params.append(category)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

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

    # One query for all images on this page instead of N+1, grouped by report.
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


def resolve_feedback(conn, report_id, response, staff):
    """Ang pag-resolve kinahanglan gyud naay tinuod nga reply, dili lang
    basta usbon ang status sa hilom.

    Returns: tuple (result, error) - 400/404 kung napakyas."""
    if not response:
        return None, (jsonify({"error": "A reply is required to mark feedback resolved"}), 400)
    if len(response) > 2000:
        return None, (jsonify({"error": "Reply must be 2000 characters or fewer"}), 400)

    report = conn.execute("SELECT * FROM feedback_reports WHERE id = ?", (report_id,)).fetchone()
    if report is None:
        return None, (jsonify({"error": "Feedback report not found"}), 404)

    conn.execute(
        """
        UPDATE feedback_reports
        SET status = 'resolved', resolved_at = datetime('now', 'localtime'),
            resolved_by_staff_id = ?, admin_response = ?
        WHERE id = ?
        """,
        (staff["id"], response, report_id),
    )
    create_feedback_resolved_notification(conn, report_id, report["staff_id"], response)
    conn.commit()

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="resolved_feedback",
        target_table="feedback_reports",
        target_id=report_id,
        details=f"response={response}",
    )

    return {"message": "Marked resolved"}, None


def reopen_feedback(conn, report_id, staff):
    """I-balik ang resolved nga feedback report sa 'open' pag-usab,
    hawanan ang mga resolution field.

    Returns: tuple (result, error) - 404 kung wala makit-i."""
    report = conn.execute("SELECT * FROM feedback_reports WHERE id = ?", (report_id,)).fetchone()
    if report is None:
        return None, (jsonify({"error": "Feedback report not found"}), 404)

    conn.execute(
        """
        UPDATE feedback_reports
        SET status = 'open', resolved_at = NULL, resolved_by_staff_id = NULL, admin_response = NULL
        WHERE id = ?
        """,
        (report_id,),
    )
    conn.commit()

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="reopened_feedback",
        target_table="feedback_reports",
        target_id=report_id,
    )

    return {"message": "Reopened"}, None
