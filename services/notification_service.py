"""In-app notification bell logic - paghimo sa notification para sa
lain-laing panghitabo (due-soon/overdue nga loan, unrestriction/
deactivation request, reservation, feedback), dungan sa read/cleared nga
state (matag staff naay kaugalingon, dili shared) ug ang Notifications
tab's own nga list/mark-read/clear/open flows.
"""

from database import get_db_connection
from utils.formatters import full_name
from utils.validators import parse_pagination_params, validate_date_string
from services.settings_service import get_due_soon_hours, is_feature_enabled
import logging

logger = logging.getLogger(__name__)

# Visible to `staff` if it's a broadcast (both recipient cols NULL) or targeted at them.
_VISIBILITY_SQL = (
    "(recipient_staff_id IS NULL AND recipient_role IS NULL)"
    " OR recipient_staff_id = ? OR recipient_role = ?"
)

# Read/cleared state is per (notification, staff), not a shared column on notifications.
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


def sync_loan_notifications():
    """Mag-himo ug 'due_soon'/'overdue' nga notification matag loan nga
    kwalipikado nga wala pa ni'y notification. Naa sa iskedyul, naay
    kaugalingong koneksyon. Kung na-overdue na ang usa ka due_soon
    notification, i-usab ra kini sa 'overdue' (dili maghimo ug bag-o), ug
    i-clear ang read state para makita pag-usab sa staff."""
    conn = get_db_connection()
    try:
        if not is_feature_enabled("transactions", conn):
            # Borrow & Return is off, so don't generate alerts for it
            return

        due_soon_hours = get_due_soon_hours(conn)

        candidates = conn.execute(
            """
            SELECT transactions.id AS transaction_id, transactions.due_date,
                   assets.name AS asset_name,
                   members.first_name, members.last_name, members.middle_name, members.suffix,
                   CASE
                       WHEN transactions.due_date < datetime('now', 'localtime') THEN 'overdue'
                       ELSE 'due_soon'
                   END AS calc_type
            FROM transactions
            JOIN assets ON assets.id = transactions.asset_id
            JOIN members ON members.id = transactions.member_id
            WHERE transactions.status = 'borrowed'
              AND transactions.due_date IS NOT NULL
              AND transactions.due_date <= datetime('now', 'localtime', ?)
            """,
            (f"+{due_soon_hours} hours",),
        ).fetchall()

        created = 0
        escalated = 0
        for row in candidates:
            member_name = full_name(
                row["first_name"], row["last_name"], row["middle_name"], row["suffix"]
            )
            if row["calc_type"] == "overdue":
                message = f"{row['asset_name']} borrowed by {member_name} is overdue"
            else:
                message = f"{row['asset_name']} borrowed by {member_name} is due soon"

            if row["calc_type"] == "overdue":
                # Escalates the row in place instead of duplicating, and clears its read state.
                existing = conn.execute(
                    "SELECT id FROM notifications WHERE transaction_id = ? AND type = 'due_soon'",
                    (row["transaction_id"],),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE notifications SET type = 'overdue', message = ? WHERE id = ?",
                        (message, existing["id"]),
                    )
                    conn.execute(
                        "DELETE FROM notification_reads WHERE notification_id = ?",
                        (existing["id"],),
                    )
                    escalated += 1
                    continue

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO notifications (type, transaction_id, message)
                VALUES (?, ?, ?)
                """,
                (row["calc_type"], row["transaction_id"], message),
            )
            if cursor.rowcount:
                created += 1

        conn.commit()
        if created or escalated:
            logger.info(
                f"Synced loan notifications: {created} created, {escalated} escalated to overdue"
            )
    except Exception:
        conn.rollback()
        logger.exception("Failed to sync loan notifications")
    finally:
        conn.close()


# Filters out muted notification types for one viewer; doesn't affect who gets notified.
PREF_FILTER_SQL = (
    "AND (type NOT IN ('due_soon') OR ? = 1)"
    " AND (type NOT IN ('overdue') OR ? = 1)"
    " AND (type NOT IN ('unrestriction_requested', 'unrestriction_resolved') OR ? = 1)"
)


def pref_filter_params(staff):
    """Tuple sa parameter para sa tulo ka placeholder sa PREF_FILTER_SQL,
    gikan sa notify_* nga column sa staff row. Returns: tuple sa 3 ka value."""
    return (
        staff["notify_due_soon"],
        staff["notify_overdue"],
        staff["notify_requests"],
    )


def get_notification_bell_context(conn, staff_id, role, staff, limit=8):
    """Mobalik ug (notifications, unread_count) para sa sidebar bell,
    sunod sa han-ay gikan sa pinakabag-o. Ang broadcast nga notification
    naay NULL ang duha ka recipient column; ang is_read/is_cleared gikan
    sa notification_reads table, per-staff man ang read state.

    Returns: tuple (notifications, unread_count)."""
    pref_params = pref_filter_params(staff)
    notifications = conn.execute(
        f"""
        SELECT notifications.id, notifications.type, notifications.transaction_id,
               notifications.unrestriction_request_id, notifications.recipient_staff_id,
               notifications.recipient_role, notifications.message, notifications.created_at,
               COALESCE(nr.is_read, 0) AS is_read,
               COALESCE(nr.is_cleared, 0) AS is_cleared
        FROM notifications
        LEFT JOIN notification_reads nr
          ON nr.notification_id = notifications.id AND nr.staff_id = ?
        WHERE COALESCE(nr.is_cleared, 0) = 0
          AND (
            (recipient_staff_id IS NULL AND recipient_role IS NULL)
            OR recipient_staff_id = ?
            OR recipient_role = ?
          )
        {PREF_FILTER_SQL}
        ORDER BY notifications.created_at DESC, notifications.id DESC
        LIMIT ?
        """,
        (staff_id, staff_id, role, *pref_params, limit),
    ).fetchall()
    unread_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM notifications
        LEFT JOIN notification_reads nr
          ON nr.notification_id = notifications.id AND nr.staff_id = ?
        WHERE COALESCE(nr.is_read, 0) = 0
          AND COALESCE(nr.is_cleared, 0) = 0
          AND (
            (recipient_staff_id IS NULL AND recipient_role IS NULL)
            OR recipient_staff_id = ?
            OR recipient_role = ?
          )
        {PREF_FILTER_SQL}
        """,
        (staff_id, staff_id, role, *pref_params),
    ).fetchone()["cnt"]
    return notifications, unread_count


# # # UNRESTRICTION REQUEST LIFECYCLE --------------------------------------
# Fire once, right when the triggering action happens - nothing to reconcile later.


def create_unrestriction_requested_notifications(conn, request_id, member_name, requester_username):
    """Pahibaw-on ang tanang admin/in_charge sa bag-ong request, usa ka
    row matag role - broadcast nga naka-target sa role, dili individual.
    Returns: None, ang nagtawag ang mag-commit."""
    message = f"{requester_username} requested unrestriction for {member_name}"
    for role in ("admin", "in_charge"):
        conn.execute(
            """
            INSERT INTO notifications (type, unrestriction_request_id, recipient_role, message)
            VALUES ('unrestriction_requested', ?, ?, ?)
            """,
            (request_id, role, message),
        )


def create_unrestriction_resolved_notification(conn, request_id, requester_staff_id, member_name, approved, review_notes=None):
    """Pahibaw-on ang nag-request nga na-approve o na-deny ang iyang
    request - DILI ipadala kung gi-cancel lang."""
    if approved:
        message = f"Your unrestriction request for {member_name} was approved"
    else:
        message = f"Your unrestriction request for {member_name} was denied"
        if review_notes:
            message += f": {review_notes}"
    conn.execute(
        """
        INSERT INTO notifications (type, unrestriction_request_id, recipient_staff_id, message)
        VALUES ('unrestriction_resolved', ?, ?, ?)
        """,
        (request_id, requester_staff_id, message),
    )


# # # RESERVATIONS --------------------------------------------------------
# One-shot, fired from services/reservation_service.py's cancel_reservation.
# No "created"/"upcoming" notification for v1 - reservations are advisory
# (see that module's docstring), so the only event worth interrupting the
# original booking staff member for is their own booking falling through.


def create_reservation_cancelled_notification(conn, reservation_id, staff_id, asset_name, member_name):
    """Pahibaw-on ang orihinal nga staff nga nag-book nga gi-cancel sa
    lain ang iyang reservation - gitawag ra kung DILI mismo siya ang
    ni-cancel."""
    conn.execute(
        """
        INSERT INTO notifications (type, reservation_id, recipient_staff_id, message)
        VALUES ('reservation_cancelled', ?, ?, ?)
        """,
        (reservation_id, staff_id, f"Reservation for {asset_name} ({member_name}) was cancelled"),
    )


# # # ADMIN DEACTIVATION REQUEST LIFECYCLE -----------------------------------
# Same one-shot shape as above - fired once from routes/staff.py.


def create_admin_deactivation_requested_notifications(conn, request_id, target_name, requester_name):
    """Pahibaw-on ang tanang in_charge/tool_keeper nga pwede mo-review,
    usa ka row matag role."""
    message = f"{requester_name} requested deactivation of admin {target_name}"
    for role in ("in_charge", "tool_keeper"):
        conn.execute(
            """
            INSERT INTO notifications (type, admin_deactivation_request_id, recipient_role, message)
            VALUES ('admin_deactivation_requested', ?, ?, ?)
            """,
            (request_id, role, message),
        )


def create_admin_deactivation_resolved_notification(conn, request_id, requester_staff_id, target_name, approved, denial_reason=None):
    """Pahibaw-on ang nag-request nga na-approve o na-deny ang iyang
    deactivation request - DILI ipadala kung gi-cancel lang."""
    if approved:
        message = f"Your request to deactivate admin {target_name} was approved"
    else:
        message = f"Your request to deactivate admin {target_name} was denied"
        if denial_reason:
            message += f": {denial_reason}"
    conn.execute(
        """
        INSERT INTO notifications (type, admin_deactivation_request_id, recipient_staff_id, message)
        VALUES ('admin_deactivation_resolved', ?, ?, ?)
        """,
        (request_id, requester_staff_id, message),
    )


# # # FEEDBACK LIFECYCLE ----------------------------------------------------
# Same one-shot shape - fired once from routes/feedback.py on submit/resolve.


def create_feedback_submitted_notifications(conn, report_id, submitter_staff_id, submitter_name, category):
    """Duha ka row: broadcast para sa tanang admin, dungan sa personal nga
    kumpirmasyon para sa nag-submit."""
    conn.execute(
        """
        INSERT INTO notifications (type, feedback_report_id, recipient_role, message)
        VALUES ('feedback_submitted', ?, 'admin', ?)
        """,
        (report_id, f"{submitter_name} submitted {category} feedback"),
    )
    conn.execute(
        """
        INSERT INTO notifications (type, feedback_report_id, recipient_staff_id, message)
        VALUES ('feedback_submitted', ?, ?, 'Your feedback was sent to the admins.')
        """,
        (report_id, submitter_staff_id),
    )


def create_feedback_resolved_notification(conn, report_id, submitter_staff_id, response):
    """Pahibaw-on ang nag-submit nga na-resolve na ang iyang report, lakip
    ang tinuod nga reply sa admin."""
    conn.execute(
        """
        INSERT INTO notifications (type, feedback_report_id, recipient_staff_id, message)
        VALUES ('feedback_resolved', ?, ?, ?)
        """,
        (report_id, submitter_staff_id, f"Your feedback was resolved: {response}"),
    )


def clear_unrestriction_pending_notifications(conn, request_id):
    """Tangtangon ang pending-review nga mga notice human ma-approve,
    ma-deny, o ma-cancel ang request."""
    conn.execute(
        "DELETE FROM notifications WHERE unrestriction_request_id = ? AND type = 'unrestriction_requested'",
        (request_id,),
    )


# # # NOTIFICATIONS TAB (list/mark-read/clear/open) -----------------------
# Everything below backs routes/notifications.py.


def get_notifications_table_context(conn, args, staff):
    """Shared query para sa notifications list, gigamit sa page ug sa
    iyang htmx partial. Ang is_read/is_cleared gikan sa LEFT JOIN nga
    naka-scope sa maong staff.

    Returns: tuple (context, error)."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    # Parenthesized: AND binds tighter than OR, so the parens keep this filter separate.
    where_clauses = [f"({_VISIBILITY_SQL})"]
    params = [staff["id"], staff["role"]]
    # Drops any type this staff member muted in Settings & privacy.
    where_clauses.append(PREF_FILTER_SQL.removeprefix("AND "))
    params.extend(pref_filter_params(staff))
    # Cleared notifications stay gone - no "show cleared" view here.
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

    return {
        "notifications": notifications,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_unread_only": show_unread_only,
        "since": since,
        "until": until,
    }, None


def mark_all_notifications_read(conn, staff):
    """Ang "Mark all as read" nga buton - i-mark read ra ang makita sa
    maong staff, dili apilon ang kopya sa uban."""
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
        (staff["id"], staff["id"], staff["id"], staff["role"], *pref_filter_params(staff)),
    )
    conn.commit()


def mark_notification_read(conn, notification_id, staff):
    """I-mark read ang usa ka notification para ani nga staff, human
    kumpirmahon nga tinuod nga makita niya - proteksyon batok sa
    pag-mark-read og notification nga dili man unta iya.

    Returns: bool - True kung malampuson."""
    notification = conn.execute(
        f"SELECT id FROM notifications WHERE id = ? AND ({_VISIBILITY_SQL})",
        (notification_id, staff["id"], staff["role"]),
    ).fetchone()
    if notification is None:
        return False

    conn.execute(_UPSERT_READ_SQL, (notification_id, staff["id"]))
    conn.commit()
    return True


def clear_notification(conn, notification_id, staff):
    """Ang '×' sa matag row - i-clear ang usa ra ka notification matag
    tawag. Returns: bool - True kung malampuson."""
    notification = conn.execute(
        f"SELECT id FROM notifications WHERE id = ? AND ({_VISIBILITY_SQL})",
        (notification_id, staff["id"], staff["role"]),
    ).fetchone()
    if notification is None:
        return False

    conn.execute(_UPSERT_CLEAR_SQL, (notification_id, staff["id"]))
    conn.commit()
    return True


def clear_notifications_bulk(conn, ids, staff):
    """Bulk clear - ang mga id nga dili makita sa staff hilom ra nga
    laktawan, dili mag-error.

    Returns: int - pila ka notification ang tinuod nga na-clear."""
    placeholders = ",".join("?" * len(ids))
    cursor = conn.execute(
        f"""
        INSERT INTO notification_reads (notification_id, staff_id, is_cleared, cleared_at)
        SELECT notifications.id, ?, 1, datetime('now', 'localtime')
        FROM notifications
        WHERE notifications.id IN ({placeholders}) AND ({_VISIBILITY_SQL})
        ON CONFLICT(notification_id, staff_id) DO UPDATE SET is_cleared = 1, cleared_at = excluded.cleared_at
        """,
        (staff["id"], *ids, staff["id"], staff["role"]),
    )
    cleared_count = cursor.rowcount
    conn.commit()
    return cleared_count


def resolve_notification_open(conn, notification_id, staff):
    """I-mark read ang notification, then ibalik kung asa dapat i-redirect
    ang staff - loans page ba o requests page, depende sa klase sa
    notification. Mobalik ug None kung wala gyud na-a ang notification, or
    kung dili siya visible/makita sa niining staff member (wrong role, o
    dili iyaha nga notification).

    Args:
        conn: open sqlite3.Connection nga gigamit para sa query.
        notification_id: id sa notification nga gi-click/gi-open sa staff.
        staff: ang staff row nga nag-open sa notification.

    Returns:
        str or None - ang URL nga i-redirect-an ("/requests",
        "/transactions?...", or "/notifications" as a last-resort
        fallback kung wala nay lain nga mahibaw-an), o None kung dili
        makita/visible ang notification sa niining staff.
    """
    notification = conn.execute(
        f"""
        SELECT notifications.id, notifications.unrestriction_request_id,
               notifications.transaction_id, transactions.member_id
        FROM notifications
        LEFT JOIN transactions ON transactions.id = notifications.transaction_id
        WHERE notifications.id = ? AND ({_VISIBILITY_SQL})
        """,
        (notification_id, staff["id"], staff["role"]),
    ).fetchone()
    if notification is None:
        return None

    conn.execute(_UPSERT_READ_SQL, (notification_id, staff["id"]))
    conn.commit()

    if notification["unrestriction_request_id"] is not None:
        return "/requests"

    if notification["transaction_id"] is not None:
        return (
            f"/transactions?member_id={notification['member_id']}"
            f"&open_transaction={notification['transaction_id']}"
        )

    # Neither kind - shouldn't happen, but a dead-end redirect beats a 500.
    return "/notifications"
