"""Query-building and mutation logic for routes/feedback.py (in-app bug/idea/other
reports staff can submit, admins can review and resolve).

Same (data, error) convention as the other *_service modules, error being a ready
`(jsonify(...), status)` tuple.

(Bisaya: Kini nga file ang naghandle sa feedback - bug/idea/other nga
report nga pwede i-submit sa staff sulod mismo sa app, mao pod ang
review/resolve nga gina-himo sa admin.)
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
    """Creates a feedback report with 0+ screenshots, all-or-nothing (a
    failed screenshot save rolls back the whole report, not a partial one).
    (Bisaya: Mo-himo ug feedback report nga naay 0 o labaw pa nga
    screenshot, tanan-o-wala - kung mapakyas ang usa ka screenshot,
    mo-rollback ang TIBUOK report, dili tugotan nga partial/kulang ra ang
    nasave.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        staff: the staff row submitting this report.
        (Bisaya: ang staff row nga nag-submit ani nga report.)
        category: one of "bug", "idea", "other".
        (Bisaya: usa sa "bug", "idea", "other".)
        message: the report text, required, max 4000 characters.
        (Bisaya: ang sulat sa report, kinahanglan gyud naay laman,
        pinakadaghan 4000 ka letra.)
        page_path: which page the staff was on when they reported this,
            optional.
        (Bisaya: unsa nga page ang gina-abrehan sa staff sa dihang
        nag-report - pwede walay laman.)
        screenshots: list of FileStorage objects, already filtered to
            non-empty by the route.
        (Bisaya: listahan sa FileStorage - mga hulagway - na-filter na sa
        route, walay laman nga file wala na dinhi.)

    Returns:
        tuple (result, error) - "message" on success, or a
        (jsonify(...), status) tuple on failure.
        (Bisaya: usa ka tuple (result, error).)
    """
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
        staff_id=staff["id"],
        action="submitted_feedback",
        target_table="feedback_reports",
        target_id=report_id,
        details=f"category={category}, screenshots={len(saved_paths)}",
    )

    return {"message": "Thanks - your feedback was sent to the admins."}, None


# # # REVIEW (admin only) ---------------------------------------------------


def get_open_feedback_count(conn=None):
    """Feeds the System/Feedback sidebar badges, same pattern as the other pending-count helpers.
    (Bisaya: Gigamit sa System/Feedback nga sidebar badge, pareho ka porma
    sa laing pending-count nga helper.)

    Args: conn (optional).
    Returns: int - pila ka feedback report ang 'open' pa.)"""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS cnt FROM feedback_reports WHERE status = 'open'").fetchone()["cnt"]
    finally:
        if owns_conn:
            conn.close()


def get_feedback_table_context(conn, args):
    """Shared query for the Feedback tab, same get_*_table_context pattern as the other tabs.
    (Bisaya: Sinagolan/shared nga query para sa Feedback tab, pareho ka
    porma sa get_*_table_context sa laing tabs.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        args: request.args-like filter input (status, category, limit,
            offset).
        (Bisaya: request.args-like nga filter input.)

    Returns:
        tuple (context, error) - `context` has reports/images_by_report/
        open_count/total_count/limit/offset/status/category; `error` for
        an invalid status or category filter.
        (Bisaya: usa ka tuple (context, error).)
    """
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
    """Resolving requires an actual reply, not a silent status flip.
    (Bisaya: Ang pag-resolve kinahanglan gyud naay tinuod nga reply -
    dili lang basta usbon ang status sa hilom.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        report_id: id of the feedback report to resolve.
        (Bisaya: id sa feedback report nga i-resolve.)
        response: the admin's reply text, required, max 2000 characters.
        (Bisaya: ang reply sa admin, kinahanglan gyud naay laman,
        pinakadaghan 2000 ka letra.)
        staff: the staff row (admin) resolving this.
        (Bisaya: ang staff row - admin - nga nag-resolve ani.)

    Returns:
        tuple (result, error) - "message" on success, or a
        (jsonify(...), status) tuple - 400/404 - on failure.
        (Bisaya: usa ka tuple (result, error).)
    """
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
        staff_id=staff["id"],
        action="resolved_feedback",
        target_table="feedback_reports",
        target_id=report_id,
        details=f"response={response}",
    )

    return {"message": "Marked resolved"}, None


def reopen_feedback(conn, report_id, staff):
    """Puts a resolved feedback report back to 'open', clearing the
    resolution fields (resolved_at, resolved_by, admin_response).
    (Bisaya: I-balik ang resolved nga feedback report sa 'open' pag-usab,
    hawanan ang mga resolution field - resolved_at, resolved_by,
    admin_response.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        report_id: id of the feedback report to reopen.
        (Bisaya: id sa feedback report nga i-reopen.)
        staff: the staff row (admin) reopening this.
        (Bisaya: ang staff row - admin - nga nag-reopen ani.)

    Returns:
        tuple (result, error) - "message" on success, or a
        (jsonify(...), 404) tuple when the report doesn't exist.
        (Bisaya: usa ka tuple (result, error).)
    """
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
        staff_id=staff["id"],
        action="reopened_feedback",
        target_table="feedback_reports",
        target_id=report_id,
    )

    return {"message": "Reopened"}, None
