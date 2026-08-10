from flask import Blueprint, jsonify, g, request, render_template, redirect
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required
from services.notification_service import PREF_FILTER_SQL, pref_filter_params
from utils.validators import parse_pagination_params, validate_date_string

notifications_bp = Blueprint("notifications", __name__)

# A row is visible to `staff` if it's a broadcast (both recipient cols NULL)
# or targeted at their id/role. See 0021_notification_recipients.sql.
_VISIBILITY_SQL = (
    "(recipient_staff_id IS NULL AND recipient_role IS NULL)"
    " OR recipient_staff_id = ? OR recipient_role = ?"
)

# Read/cleared state is per (notification, staff), not a column on notifications
# itself (that'd be shared across everyone who sees a broadcast row). No
# notification_reads row = default state: unread, not cleared. See 0027_notification_per_user_read_state.sql.
_UPSERT_READ_SQL = """
    INSERT INTO notification_reads (notification_id, staff_id, is_read, read_at)
    VALUES (?, ?, 1, datetime('now', 'localtime'))
    ON CONFLICT(notification_id, staff_id) DO UPDATE SET is_read = 1, read_at = excluded.read_at
"""
_UPSERT_CLEAR_SQL = """
    INSERT INTO notification_reads (notification_id, staff_id, is_cleared, cleared_at)
    VALUES (?, ?, 1, datetime('now', 'localtime'))
    ON CONFLICT(notification_id, staff_id) DO UPDATE SET is_cleared = 1, cleared_at = excluded.cleared_at
"""


def get_notifications_table_context(args, staff):
    """Shared query logic for the notifications list -- used by the full
    /notifications page and the htmx partial below (same get_*_table_context
    pattern as assets/members/staff/transactions). Returns (context_dict, error).

    is_read/is_cleared come from a LEFT JOIN on notification_reads scoped to
    this staff member; explicit column list keeps those aliases from colliding
    with the old same-named columns still on the notifications table."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    # Parenthesized: AND binds tighter than OR, so without the parens only
    # the last disjunct would get ANDed with the other filters.
    where_clauses = [f"({_VISIBILITY_SQL})"]
    params = [staff["id"], staff["role"]]
    # Drops any type this staff member muted in Settings & privacy.
    where_clauses.append(PREF_FILTER_SQL.removeprefix("AND "))
    params.extend(pref_filter_params(staff))
    # Cleared notifications stay gone -- no "show cleared" view here.
    where_clauses.append("COALESCE(nr.is_cleared, 0) = 0")

    show_unread_only = args.get("show_unread_only") == "1"
    if show_unread_only:
        where_clauses.append("COALESCE(nr.is_read, 0) = 0")

    # Filters on the date portion of created_at, inclusive both ends.
    since = args.get("since")
    if since:
        error = validate_date_string(since, "since")
        if error:
            return None, error
        where_clauses.append("date(notifications.created_at) >= ?")
        params.append(since)

    until = args.get("until")
    if until:
        error = validate_date_string(until, "until")
        if error:
            return None, error
        where_clauses.append("date(notifications.created_at) <= ?")
        params.append(until)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    join_sql = "LEFT JOIN notification_reads nr ON nr.notification_id = notifications.id AND nr.staff_id = ?"
    join_params = [staff["id"]]

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM notifications {join_sql} {where_sql}",
        (*join_params, *params),
    ).fetchone()["cnt"]
    notifications = conn.execute(
        f"""
        SELECT notifications.id, notifications.type, notifications.transaction_id,
               notifications.unrestriction_request_id, notifications.recipient_staff_id,
               notifications.recipient_role, notifications.message, notifications.created_at,
               COALESCE(nr.is_read, 0) AS is_read,
               COALESCE(nr.is_cleared, 0) AS is_cleared
        FROM notifications
        {join_sql}
        {where_sql}
        ORDER BY notifications.created_at DESC, notifications.id DESC
        LIMIT ? OFFSET ?
        """,
        (*join_params, *params, limit, offset),
    ).fetchall()
    conn.close()

    return {
        "notifications": notifications,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_unread_only": show_unread_only,
        "since": since,
        "until": until,
    }, None


@notifications_bp.route("/notifications/table")
@login_required
def view_notifications_table():
    """htmx partial -- just the list + pagination footer, so filtering/paging
    doesn't re-render the whole page."""
    context, error = get_notifications_table_context(request.args, g.staff)
    if error:
        return error
    return render_template("notifications/_table.html", **context)


@notifications_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    """Marks read only what this staff member can see -- without the
    visibility filter a tool_keeper could mark admin-only notifications
    read. Writes one notification_reads row per staff_id, so it never
    touches anyone else's copy of a broadcast notification."""
    conn = get_db_connection()
    conn.execute(
        f"""
        INSERT INTO notification_reads (notification_id, staff_id, is_read, read_at)
        SELECT notifications.id, ?, 1, datetime('now', 'localtime')
        FROM notifications
        LEFT JOIN notification_reads nr
          ON nr.notification_id = notifications.id AND nr.staff_id = ?
        WHERE COALESCE(nr.is_read, 0) = 0
          AND COALESCE(nr.is_cleared, 0) = 0
          AND ({_VISIBILITY_SQL})
          {PREF_FILTER_SQL}
        ON CONFLICT(notification_id, staff_id) DO UPDATE SET is_read = 1, read_at = excluded.read_at
        """,
        (g.staff["id"], g.staff["id"], g.staff["id"], g.staff["role"], *pref_filter_params(g.staff)),
    )
    conn.commit()
    conn.close()
    return jsonify({"message": "All notifications marked as read"})


@notifications_bp.route("/notifications/<int:id>/mark-read", methods=["POST"])
@login_required
def mark_notification_read(id):
    conn = get_db_connection()
    notification = conn.execute(
        f"SELECT id FROM notifications WHERE id = ? AND ({_VISIBILITY_SQL})",
        (id, g.staff["id"], g.staff["role"]),
    ).fetchone()
    if notification is None:
        conn.close()
        return jsonify({"error": "Notification not found"}), 404

    conn.execute(_UPSERT_READ_SQL, (id, g.staff["id"]))
    conn.commit()
    conn.close()
    return jsonify({"message": "Notification marked as read"})


# # # CLEARING ---------------------------------------------------------------
# Clearing is the only way a notification leaves the bell dropdown or full list now
# (no longer auto-disappears when the loan resolves), and it's per staff member --
# clearing a broadcast doesn't clear it for anyone else.


@notifications_bp.route("/notifications/<int:id>/clear", methods=["POST"])
@login_required
def clear_notification(id):
    """The '×' on each row -- clears one notification at a time."""
    conn = get_db_connection()
    notification = conn.execute(
        f"SELECT id FROM notifications WHERE id = ? AND ({_VISIBILITY_SQL})",
        (id, g.staff["id"], g.staff["role"]),
    ).fetchone()
    if notification is None:
        conn.close()
        return jsonify({"error": "Notification not found"}), 404

    conn.execute(_UPSERT_CLEAR_SQL, (id, g.staff["id"]))
    conn.commit()
    conn.close()
    return jsonify({"message": "Notification cleared"})


@notifications_bp.route("/notifications/clear", methods=["POST"])
@login_required
def clear_notifications():
    """"Clear selected" bulk action. Body: {"ids": [1, 2, 3]}. Same visibility
    scoping as everywhere else, so ids the staff member can't see are just
    silently skipped rather than erroring (a stale checkbox from another
    tab isn't worth failing the whole request over)."""
    data, error = get_json_or_error()
    if error:
        return error

    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids must be a non-empty list"}), 400
    if not all(isinstance(i, int) for i in ids):
        return jsonify({"error": "ids must be a list of integers"}), 400

    conn = get_db_connection()
    placeholders = ",".join("?" * len(ids))
    cursor = conn.execute(
        f"""
        INSERT INTO notification_reads (notification_id, staff_id, is_cleared, cleared_at)
        SELECT notifications.id, ?, 1, datetime('now', 'localtime')
        FROM notifications
        WHERE notifications.id IN ({placeholders}) AND ({_VISIBILITY_SQL})
        ON CONFLICT(notification_id, staff_id) DO UPDATE SET is_cleared = 1, cleared_at = excluded.cleared_at
        """,
        (g.staff["id"], *ids, g.staff["id"], g.staff["role"]),
    )
    cleared_count = cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify({"message": f"{cleared_count} notification(s) cleared", "cleared_count": cleared_count})


@notifications_bp.route("/notifications/<int:id>/open")
@login_required
def open_notification(id):
    """Click target for a notification: marks it read, then redirects
    depending on notification type. Loan alerts (transaction_id set) go to
    Borrow & Return filtered to that borrower with the specific loan's modal
    auto-opened; unrestriction requests (unrestriction_request_id set) go to
    Approvals. Deliberately a plain GET even though it has a side effect
    (marks read) -- same "click it, it opens and clears" pattern email
    clients use, simpler than duplicating the redirect logic in JS."""
    conn = get_db_connection()
    notification = conn.execute(
        f"""
        SELECT notifications.id, notifications.unrestriction_request_id,
               notifications.transaction_id, transactions.member_id
        FROM notifications
        LEFT JOIN transactions ON transactions.id = notifications.transaction_id
        WHERE notifications.id = ? AND ({_VISIBILITY_SQL})
        """,
        (id, g.staff["id"], g.staff["role"]),
    ).fetchone()
    if notification is None:
        conn.close()
        return redirect("/notifications")

    conn.execute(_UPSERT_READ_SQL, (id, g.staff["id"]))
    conn.commit()
    conn.close()

    if notification["unrestriction_request_id"] is not None:
        return redirect("/requests")

    # Falls through to /notifications for any row that's neither kind --
    # shouldn't happen, but a dead-end redirect beats a 500.
    if notification["transaction_id"] is not None:
        return redirect(
            f"/transactions?member_id={notification['member_id']}"
            f"&open_transaction={notification['transaction_id']}"
        )

    return redirect("/notifications")
