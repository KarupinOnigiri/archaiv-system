"""Query-building and mutation logic for routes/requests.py (member unrestriction
requests - the tool_keeper-files, admin/in_charge-reviews approval flow).

Same convention as the other *_service modules: functions take an already-open
`conn` and return `(data, error)`, where `error` is `None` on success or a ready
`(jsonify(...), status)` tuple on failure.
"""

import logging
from datetime import datetime

from flask import jsonify

from database import sqlite3
from services.notification_service import (
    create_unrestriction_requested_notifications,
    create_unrestriction_resolved_notification,
    clear_unrestriction_pending_notifications,
)
from utils.formatters import full_name
from utils.logger import log_action

logger = logging.getLogger(__name__)

VALID_STATUSES = ("pending", "approved", "denied", "cancelled")


def create_unrestriction_request(conn, member_id, reason, staff):
    """tool_keeper can't unrestrict directly, they file a request that admin/in_charge reviews."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()

    if member is None:
        return None, (jsonify({"error": "Member not found"}), 404)

    if not member["is_restricted"]:
        return None, (jsonify({"error": "Member is not currently restricted"}), 400)

    existing_pending = conn.execute(
        "SELECT id FROM unrestriction_requests WHERE member_id = ? AND status = 'pending'",
        (member_id,),
    ).fetchone()

    if existing_pending is not None:
        return None, (
            jsonify(
                {
                    "error": "A pending unrestriction request already exists for this member",
                    "request_id": existing_pending["id"],
                }
            ),
            409,
        )

    updated_name = full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"])

    try:
        cursor = conn.execute(
            "INSERT INTO unrestriction_requests (member_id, requested_by_staff_id, reason) VALUES (?, ?, ?)",
            (member_id, staff["id"], reason),
        )
        request_id = cursor.lastrowid
        # Same transaction as the insert, so request and notification succeed or fail together.
        create_unrestriction_requested_notifications(conn, request_id, updated_name, staff["username"])
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, (jsonify({"error": "A database constraint was violated."}), 400)

    log_action(
        staff_id=staff["id"],
        action="requested_unrestriction",
        target_table="unrestriction_requests",
        target_id=request_id,
        target_name=updated_name,
        details=f"Requested unrestriction for member {updated_name}: {reason}",
    )

    return {"message": "Unrestriction request submitted", "id": request_id}, None


def _load_pending_request(id, conn):
    """Returns (request_row, error_response). error_response is None if
    the request exists and is still pending."""
    req = conn.execute("SELECT * FROM unrestriction_requests WHERE id = ?", (id,)).fetchone()

    if req is None:
        return None, (jsonify({"error": "Request not found"}), 404)

    if req["status"] != "pending":
        return None, (jsonify({"error": f"This request has already been {req['status']}"}), 409)

    return req, None


def cancel_unrestriction_request(conn, request_id, review_notes, staff):
    """Lets the requester withdraw their own pending request (unlike deny, which is admin/in_charge only)."""
    req, error = _load_pending_request(request_id, conn)
    if error:
        return None, error

    if staff["id"] != req["requested_by_staff_id"]:
        return None, (jsonify({"error": "Only the staff member who filed this request can cancel it"}), 403)

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        conn.execute(
            """
            UPDATE unrestriction_requests
            SET status = 'cancelled', reviewed_by_staff_id = ?, review_notes = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (staff["id"], review_notes, reviewed_at, request_id),
        )
        # Requester doesn't need a notice, but clears admin/in_charge's pending-review one.
        clear_unrestriction_pending_notifications(conn, request_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # req only has member_id; look up the name here for a readable log entry.
    cancel_req_member = conn.execute(
        "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?",
        (req["member_id"],),
    ).fetchone()

    log_action(
        staff_id=staff["id"],
        action="cancelled_unrestriction_request",
        target_table="unrestriction_requests",
        target_id=request_id,
        target_name=(
            full_name(cancel_req_member['first_name'], cancel_req_member['last_name'], cancel_req_member['middle_name'], cancel_req_member['suffix'])
            if cancel_req_member else None
        ),
        details=f"Requester cancelled their own unrestriction request #{request_id}"
        + (f": {review_notes}" if review_notes else ""),
    )

    return {"message": "Request cancelled"}, None


def get_unrestriction_requests_context(conn, args, limit, offset, staff):
    """admin/in_charge see the full review queue; tool_keeper only sees their own requests."""
    status_filter = args.get("status")
    if status_filter and status_filter not in VALID_STATUSES:
        return None, (jsonify({"error": f"status must be one of: {', '.join(VALID_STATUSES)}"}), 400)

    where_clauses = []
    params = []

    if staff["role"] not in ("admin", "in_charge"):
        where_clauses.append("unrestriction_requests.requested_by_staff_id = ?")
        params.append(staff["id"])
    elif args.get("mine") == "1":
        # Opt-in filter for admin/in_charge to narrow the queue to just their own filings.
        where_clauses.append("unrestriction_requests.requested_by_staff_id = ?")
        params.append(staff["id"])

    if status_filter:
        where_clauses.append("unrestriction_requests.status = ?")
        params.append(status_filter)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM unrestriction_requests {where_sql}", params
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT unrestriction_requests.*,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.restriction_reason AS member_restriction_reason,
               members.restriction_source AS member_restriction_source,
               requester.username AS requested_by_username,
               reviewer.username AS reviewed_by_username
        FROM unrestriction_requests
        JOIN members ON unrestriction_requests.member_id = members.id
        JOIN staff AS requester ON unrestriction_requests.requested_by_staff_id = requester.id
        LEFT JOIN staff AS reviewer ON unrestriction_requests.reviewed_by_staff_id = reviewer.id
        {where_sql}
        ORDER BY unrestriction_requests.created_at DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "status_filter": status_filter or "",
        "requests_list": rows,
    }, None


def get_unrestriction_request_detail(conn, request_id, staff):
    """Single-row version of the same join get_unrestriction_requests_context
    uses, for the Approvals table's own "click a row for full details" modal.
    tool_keeper can only open their own filed requests - same visibility rule
    list_unrestriction_requests already applies to the whole list."""
    row = conn.execute(
        """
        SELECT unrestriction_requests.*,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.restriction_reason AS member_restriction_reason,
               members.restriction_source AS member_restriction_source,
               requester.username AS requested_by_username,
               reviewer.username AS reviewed_by_username
        FROM unrestriction_requests
        JOIN members ON unrestriction_requests.member_id = members.id
        JOIN staff AS requester ON unrestriction_requests.requested_by_staff_id = requester.id
        LEFT JOIN staff AS reviewer ON unrestriction_requests.reviewed_by_staff_id = reviewer.id
        WHERE unrestriction_requests.id = ?
        """,
        (request_id,),
    ).fetchone()

    if row is None:
        return None, (jsonify({"error": "Unrestriction request not found"}), 404)
    if staff["role"] not in ("admin", "in_charge") and row["requested_by_staff_id"] != staff["id"]:
        return None, (jsonify({"error": "You can only view your own requests"}), 403)

    return row, None


def approve_unrestriction_request(conn, request_id, review_notes, staff):
    req, error = _load_pending_request(request_id, conn)
    if error:
        return None, error

    member = conn.execute("SELECT * FROM members WHERE id = ?", (req["member_id"],)).fetchone()

    if member is None:
        return None, (jsonify({"error": "Member no longer exists"}), 404)

    if staff["member_id"] == req["member_id"]:
        return None, (
            jsonify(
                {
                    "error": "You cannot approve an unrestriction request for your own account.",
                    "reason": "Ask another admin or in_charge to review it.",
                }
            ),
            403,
        )

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated_name = full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"])

    try:
        # same effect as the direct unrestrict_member route, just gated behind a second set of eyes
        conn.execute(
            "UPDATE members SET is_restricted = 0, restriction_reason = NULL, restriction_source = NULL WHERE id = ?",
            (req["member_id"],),
        )
        conn.execute(
            """
            UPDATE unrestriction_requests
            SET status = 'approved', reviewed_by_staff_id = ?, review_notes = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (staff["id"], review_notes, reviewed_at, request_id),
        )
        clear_unrestriction_pending_notifications(conn, request_id)
        create_unrestriction_resolved_notification(conn, request_id, req["requested_by_staff_id"], updated_name, approved=True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        staff_id=staff["id"],
        action="approved_unrestriction_request",
        target_table="unrestriction_requests",
        target_id=request_id,
        target_name=updated_name,
        details=f"Approved unrestriction for member {updated_name}",
    )

    return {"message": "Request approved, member unrestricted"}, None


def deny_unrestriction_request(conn, request_id, review_notes, staff):
    req, error = _load_pending_request(request_id, conn)
    if error:
        return None, error

    member = conn.execute("SELECT * FROM members WHERE id = ?", (req["member_id"],)).fetchone()
    updated_name = (
        full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"])
        if member is not None
        else "the member"
    )

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        conn.execute(
            """
            UPDATE unrestriction_requests
            SET status = 'denied', reviewed_by_staff_id = ?, review_notes = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (staff["id"], review_notes, reviewed_at, request_id),
        )
        clear_unrestriction_pending_notifications(conn, request_id)
        create_unrestriction_resolved_notification(
            conn, request_id, req["requested_by_staff_id"], updated_name, approved=False, review_notes=review_notes
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        staff_id=staff["id"],
        action="denied_unrestriction_request",
        target_table="unrestriction_requests",
        target_id=request_id,
        target_name=updated_name,
        details=f"Denied unrestriction request: {review_notes}",
    )

    return {"message": "Request denied"}, None
