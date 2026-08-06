from flask import Blueprint, jsonify, request, render_template, redirect
from database import get_db_connection
from services.auth_service import login_required
from utils.validators import parse_pagination_params, validate_date_string

notifications_bp = Blueprint("notifications", __name__)


def get_notifications_table_context(args):
    """Shared query logic for the notifications list -- used by both the
    full /notifications page (routes/pages.py) and the htmx partial
    below, the same get_*_table_context pattern used by assets/members/
    staff/transactions. Returns (context_dict, error)."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    where_clauses = []
    params = []

    show_unread_only = args.get("show_unread_only") == "1"
    if show_unread_only:
        where_clauses.append("is_read = 0")

    # Mirrors routes/staff.py's get_audit_log_context: filters on the
    # date portion of created_at, inclusive on both ends.
    since = args.get("since")
    if since:
        error = validate_date_string(since, "since")
        if error:
            return None, error
        where_clauses.append("date(created_at) >= ?")
        params.append(since)

    until = args.get("until")
    if until:
        error = validate_date_string(until, "until")
        if error:
            return None, error
        where_clauses.append("date(created_at) <= ?")
        params.append(until)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM notifications {where_sql}", params
    ).fetchone()["cnt"]
    notifications = conn.execute(
        f"""
        SELECT * FROM notifications
        {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
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
    """htmx partial -- returns just the list + pagination footer, so
    filtering/paging/sorting never re-renders the whole page (matching
    the assets/members/staff/transactions pattern)."""
    context, error = get_notifications_table_context(request.args)
    if error:
        return error
    return render_template("notifications/_table.html", **context)


@notifications_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    conn = get_db_connection()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")
    conn.commit()
    conn.close()
    return jsonify({"message": "All notifications marked as read"})


@notifications_bp.route("/notifications/<int:id>/mark-read", methods=["POST"])
@login_required
def mark_notification_read(id):
    conn = get_db_connection()
    notification = conn.execute(
        "SELECT * FROM notifications WHERE id = ?", (id,)
    ).fetchone()
    if notification is None:
        conn.close()
        return jsonify({"error": "Notification not found"}), 404

    conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Notification marked as read"})


@notifications_bp.route("/notifications/<int:id>/open")
@login_required
def open_notification(id):
    """The click target for a notification, from both the bell dropdown
    and the full /notifications page: marks it read, then redirects to
    the Borrow & Return tab filtered to that borrower's loans
    (?member_id=<id>, an exact-match filter -- see
    _build_transactions_filters in routes/transactions.py) with
    ?open_transaction=<id> on top, which auto-opens that one loan's
    existing details modal. So the destination shows every loan this
    borrower currently has out (useful context -- an overdue member
    often has more than one item outstanding) with the specific one the
    notification was about immediately front-and-center in its modal.
    Works even if that loan was returned moments ago and the scheduled
    sync hasn't cleared this notification yet, since both the filter and
    the details modal look it up regardless of current status.

    Deliberately a GET (so it can be a plain <a href>, working identically
    in both places without duplicated JS) even though it has the side
    effect of marking the row read -- a deviation from strict REST, but
    the standard, low-stakes pattern for "click a notification, it opens
    and clears" (email clients do the same)."""
    conn = get_db_connection()
    notification = conn.execute(
        """
        SELECT notifications.*, transactions.member_id
        FROM notifications
        JOIN transactions ON transactions.id = notifications.transaction_id
        WHERE notifications.id = ?
        """,
        (id,),
    ).fetchone()
    if notification is None:
        conn.close()
        return redirect("/notifications")

    conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(
        f"/transactions?member_id={notification['member_id']}"
        f"&open_transaction={notification['transaction_id']}"
    )
