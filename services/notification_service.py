"""In-app notification bell logic: creating notifications for various events
(due-soon/overdue loans, unrestriction/deactivation requests, reservations,
feedback), plus the read/cleared state (per staff member, not shared) and
the Notifications tab's own list/mark-read/clear/open flows.

(Bisaya: Kini nga file ang naghandle sa notification bell sa app - paghimo
sa notification para sa lain-laing panghitabo - due-soon/overdue nga loan,
unrestriction/deactivation request, reservation, feedback - dungan sa
read/cleared nga state - matag staff naay kaugalingon, dili shared - ug
ang Notifications tab's own nga list/mark-read/clear/open.)
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
    """Creates a 'due_soon'/'overdue' notification per qualifying loan that doesn't already have one.
    Runs on a schedule, opening its own connection like the other jobs.
    (Bisaya: Mag-himo ug 'due_soon'/'overdue' nga notification matag loan
    nga kwalipikado, kung wala pa ni'y notification. Naa sa iskedyul,
    naay kaugalingong koneksyon pareho sa ubang scheduled job.

    Walay parameter ug walay return value. Kung na-overdue na ang usa ka
    due_soon notification, i-usab ra kini sa 'overdue' - dili maghimo ug
    bag-o - ug i-clear ang read state niini, para makita pag-usab sa
    staff nga na-eskalar na ni.)"""
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
    """Param tuple for PREF_FILTER_SQL's three placeholders, from a staff row's notify_* columns.
    (Bisaya: Tuple sa parameter para sa tulo ka placeholder sa
    PREF_FILTER_SQL, gikan sa notify_* nga column sa staff row.)

    Args: staff (row nga naay notify_due_soon/notify_overdue/notify_requests).
    Returns: tuple sa 3 ka value (0 o 1 matag usa).)"""
    return (
        staff["notify_due_soon"],
        staff["notify_overdue"],
        staff["notify_requests"],
    )


def get_notification_bell_context(conn, staff_id, role, staff, limit=8):
    """Returns (notifications, unread_count) for the sidebar bell, most recent first.
    Broadcasts have both recipient columns NULL; is_read/is_cleared come from notification_reads, not notifications itself.
    (Bisaya: Mobalik ug (notifications, unread_count) para sa sidebar bell,
    sunod sa han-ay gikan sa pinakabag-o. Ang "broadcast" nga notification
    - para sa tanan - naay NULL ang duha ka recipient column; ang
    is_read/is_cleared gikan sa notification_reads table, dili sa
    notifications mismo - kay per-staff man ang read state.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        staff_id: id of the staff member viewing the bell.
        (Bisaya: id sa staff nga nagtan-aw sa bell.)
        role: that staff member's role - for role-targeted broadcasts.
        (Bisaya: role sa maong staff - para sa mga broadcast nga
        gina-target og role.)
        staff: the staff row itself, needed for pref_filter_params().
        (Bisaya: ang staff row mismo, gikinahanglan sa
        pref_filter_params().)
        limit: max notifications to return, defaults to 8.
        (Bisaya: pinakadaghan nga notification nga ibalik, default 8.)

    Returns:
        tuple (notifications, unread_count) - `notifications` a list of
        rows (uncleared, visible to this staff, respecting mute
        preferences); `unread_count` the badge number.
        (Bisaya: usa ka tuple (notifications, unread_count) -
        `notifications` listahan sa row nga wala pa cleared, makita sa
        maong staff, gi-respeto ang mute preference; `unread_count` ang
        numero sa badge.)
    """
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
    """Notifies every admin/in_charge of a new request, one row per role.
    (Bisaya: Pahibaw-on ang tanang admin/in_charge sa bag-ong request, usa
    ka row matag role - broadcast nga naka-target sa role, dili sa
    individual nga staff.)
    Args: conn, request_id, member_name, requester_username.
    Returns: None - i-insert lang ang rows, ang nagtawag mag-commit."""
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
    """Notifies the requester their request was approved/denied; not sent on cancellation.
    (Bisaya: Pahibaw-on ang nag-request nga na-approve o na-deny ang iyang
    request - DILI ipadala kung gi-cancel lang.)
    Args: conn, request_id, requester_staff_id, member_name, approved
    (bool), review_notes (optional). Returns: None."""
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
    """Notifies the original booking staff member that someone else cancelled their reservation.
    (Bisaya: Pahibaw-on ang orihinal nga staff nga nag-book nga gi-cancel
    sa lain ang iyang reservation - gitawag ra kini kung DILI mismo siya
    ang ni-cancel.)
    Args: conn, reservation_id, staff_id (recipient), asset_name, member_name.
    Returns: None."""
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
    """Notifies every in_charge/tool_keeper eligible to review, one row per role.
    (Bisaya: Pahibaw-on ang tanang in_charge/tool_keeper nga pwede
    mo-review, usa ka row matag role.)
    Args: conn, request_id, target_name, requester_name. Returns: None."""
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
    """Notifies the requester their deactivation request was approved/denied; not sent on cancellation.
    (Bisaya: Pahibaw-on ang nag-request nga na-approve o na-deny ang iyang
    deactivation request - DILI ipadala kung gi-cancel lang.)
    Args: conn, request_id, requester_staff_id, target_name, approved
    (bool), denial_reason (optional). Returns: None."""
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
    """Two rows: a broadcast to every admin, plus a personal confirmation to the submitter.
    (Bisaya: Duha ka row: broadcast para sa tanang admin, dungan sa
    personal nga kumpirmasyon para sa nag-submit.)
    Args: conn, report_id, submitter_staff_id, submitter_name, category.
    Returns: None."""
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
    """Notifies the submitter their report was resolved, with the admin's actual reply.
    (Bisaya: Pahibaw-on ang nag-submit nga na-resolve na ang iyang report,
    lakip ang tinuod nga reply sa admin.)
    Args: conn, report_id, submitter_staff_id, response. Returns: None."""
    conn.execute(
        """
        INSERT INTO notifications (type, feedback_report_id, recipient_staff_id, message)
        VALUES ('feedback_resolved', ?, ?, ?)
        """,
        (report_id, submitter_staff_id, f"Your feedback was resolved: {response}"),
    )


def clear_unrestriction_pending_notifications(conn, request_id):
    """Removes pending-review notices once a request is approved, denied, or cancelled.
    (Bisaya: Tangtangon ang pending-review nga mga notice human ma-approve,
    ma-deny, o ma-cancel ang request.)
    Args: conn, request_id. Returns: None."""
    conn.execute(
        "DELETE FROM notifications WHERE unrestriction_request_id = ? AND type = 'unrestriction_requested'",
        (request_id,),
    )


# # # NOTIFICATIONS TAB (list/mark-read/clear/open) -----------------------
# Everything below backs routes/notifications.py.


def get_notifications_table_context(conn, args, staff):
    """Shared query for the notifications list, used by the page and its htmx partial.
    is_read/is_cleared come from a LEFT JOIN scoped to this staff member.
    (Bisaya: Sinagolan/shared nga query para sa notifications list,
    gigamit sa page ug sa iyang htmx partial. Ang is_read/is_cleared gikan
    sa LEFT JOIN nga naka-scope sa maong staff.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        args: request.args-like filter input (show_unread_only, since,
            until, limit, offset).
        (Bisaya: request.args-like nga filter input.)
        staff: the staff row viewing the list.
        (Bisaya: ang staff row nga nagtan-aw sa listahan.)

    Returns:
        tuple (context, error) - `context` has notifications/total_count/
        limit/offset/show_unread_only/since/until; `error` for an invalid
        date filter.
        (Bisaya: usa ka tuple (context, error).)
    """
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
    """Marks read only what this staff member can see, without touching anyone else's copy.
    (Bisaya: Ang "Mark all as read" nga buton - i-mark read ra ang makita
    sa maong staff, dili apilon ang kopya sa uban - kay per-staff man ang
    read state.)
    Args: conn, staff. Returns: None - mag-commit dayon."""
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
    """Marks one notification read for this staff member, after confirming
    it's actually visible to them (prevents marking-read something not
    even theirs to see).
    (Bisaya: I-mark read ang usa ka notification para ani nga staff, human
    kumpirmahon nga tinuod nga makita niya kini - proteksyon batok sa
    pag-mark-read og notification nga dili man unta iya.)

    Args: conn, notification_id, staff.
    Returns: bool - True kung malampuson, False kung wala makit-i o dili
    makita sa maong staff."""
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
    """The '×' on each row - clears one notification at a time.
    (Bisaya: Ang '×' sa matag row - i-clear ang usa ra ka notification
    matag tawag.)
    Args: conn, notification_id, staff.
    Returns: bool - True kung malampuson, False kung wala makit-i o dili
    makita sa maong staff."""
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
    """Bulk clear; ids the staff member can't see are silently skipped rather than erroring.
    (Bisaya: Bulk/daghan dungan nga clear - ang mga id nga dili makita sa
    staff hilom ra nga laktawan, dili mag-error.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        ids: list of notification ids to clear.
        (Bisaya: listahan sa mga notification id nga i-clear.)
        staff: the staff member clearing them.
        (Bisaya: ang staff nga nag-clear.)

    Returns:
        int - how many were actually cleared (visible ones only).
        (Bisaya: numero - pila ka notification ang tinuod nga na-clear -
        ang makita ra ni-a niya.)
    """
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
