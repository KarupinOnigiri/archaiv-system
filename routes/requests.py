from flask import Blueprint, jsonify, g, request, render_template
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from utils.formatters import full_name
from utils.validators import parse_pagination_params
from services.notification_service import (
    create_unrestriction_requested_notifications,
    create_unrestriction_resolved_notification,
    clear_unrestriction_pending_notifications,
)
from datetime import datetime
import sqlite3
from extensions import limiter
import logging

logger = logging.getLogger(__name__)

requests_bp = Blueprint("requests", __name__)


VALID_STATUSES = ("pending", "approved", "denied", "cancelled")


# # # CREATE (tool_keeper flags a restricted member for review) -----------


@requests_bp.route("/unrestriction-requests", methods=["POST"])
@limiter.limit("20 per minute")
@roles_required("tool_keeper")
def create_unrestriction_request():
    """tool_keeper cannot unrestrict directly — this is the request path
    instead. admin/in_charge review it via approve/deny below."""
    data, error = get_json_or_error()
    if error:
        return error

    member_id = data.get("member_id")
    reason = data.get("reason")

    if not member_id:
        return jsonify({"error": "Missing required field: member_id"}), 400
    if not reason:
        return jsonify({"error": "Missing required field: reason"}), 400

    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    if not member["is_restricted"]:
        conn.close()
        return jsonify({"error": "Member is not currently restricted"}), 400

    existing_pending = conn.execute(
        "SELECT id FROM unrestriction_requests WHERE member_id = ? AND status = 'pending'",
        (member_id,),
    ).fetchone()

    if existing_pending is not None:
        conn.close()
        return (
            jsonify(
                {
                    "error": "A pending unrestriction request already exists for this member",
                    "request_id": existing_pending["id"],
                }
            ),
            409,
        )

    updated_name = full_name(
        member["first_name"],
        member["last_name"],
        member["middle_name"],
        member["suffix"],
    )

    try:
        cursor = conn.execute(
            """
            INSERT INTO unrestriction_requests (member_id, requested_by_staff_id, reason)
            VALUES (?, ?, ?)
            """,
            (member_id, g.staff["id"], reason),
        )
        request_id = cursor.lastrowid
        # Same transaction as the request insert -- an admin/in_charge
        # should never be able to observe a request that exists but has no
        # notification behind it (or vice versa) if something failed
        # partway through.
        create_unrestriction_requested_notifications(
            conn, request_id, updated_name, g.staff["username"]
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return jsonify({"error": "A database constraint was violated."}), 400
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="requested_unrestriction",
        target_table="unrestriction_requests",
        target_id=request_id,
        target_name=updated_name,
        details=f"Requested unrestriction for member {updated_name}: {reason}",
    )

    return (
        jsonify({"message": "Unrestriction request submitted", "id": request_id}),
        201,
    )


@requests_bp.route("/unrestriction-requests/<int:id>/cancel", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def cancel_unrestriction_request(id):
    """Lets the original requester withdraw their own pending request.
    Distinct from /deny (admin/in_charge only) since the permission check
    here is different: it's about who filed it, not seniority."""
    data, error = get_json_or_error()
    review_notes = data.get("review_notes") if not error else None

    conn = get_db_connection()
    req, error = _load_pending_request(id, conn)
    if error:
        conn.close()
        return error

    if g.staff["id"] != req["requested_by_staff_id"]:
        conn.close()
        return (
            jsonify(
                {"error": "Only the staff member who filed this request can cancel it"}
            ),
            403,
        )

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        conn.execute(
            """
            UPDATE unrestriction_requests
            SET status = 'cancelled', reviewed_by_staff_id = ?, review_notes = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (g.staff["id"], review_notes, reviewed_at, id),
        )
        # The requester already knows they cancelled it -- no notification
        # back to them -- but admin/in_charge's "pending review" notices
        # for it need to go, since there's nothing left to review.
        clear_unrestriction_pending_notifications(conn, id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        # req itself (a plain SELECT * from unrestriction_requests) has no
        # member name, only member_id -- looked up here before conn.close()
        # purely for a readable Activity Log target.
        cancel_req_member = conn.execute(
            "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?",
            (req["member_id"],),
        ).fetchone()
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="cancelled_unrestriction_request",
        target_table="unrestriction_requests",
        target_id=id,
        target_name=(
            full_name(cancel_req_member['first_name'], cancel_req_member['last_name'], cancel_req_member['middle_name'], cancel_req_member['suffix'])
            if cancel_req_member else None
        ),
        details=f"Requester cancelled their own unrestriction request #{id}"
        + (f": {review_notes}" if review_notes else ""),
    )

    return jsonify({"message": "Request cancelled"})


# # # LIST (the "tab") ----------------------------------------------------


@requests_bp.route("/unrestriction-requests")
@login_required
def list_unrestriction_requests():
    """admin/in_charge see everyone's requests (the review queue).
    tool_keeper only sees their own — self-service status tracking,
    not a window into everyone else's activity."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    status_filter = request.args.get("status")
    if status_filter and status_filter not in VALID_STATUSES:
        return (
            jsonify({"error": f"status must be one of: {', '.join(VALID_STATUSES)}"}),
            400,
        )

    conn = get_db_connection()

    where_clauses = []
    params = []

    if g.staff["role"] not in ("admin", "in_charge"):
        where_clauses.append("unrestriction_requests.requested_by_staff_id = ?")
        params.append(g.staff["id"])
    elif request.args.get("mine") == "1":
        # admin/in_charge see everyone's by default (the review queue) --
        # this narrows that back down to "requests I personally filed",
        # for the same admin/in_charge account that might also be the one
        # who flagged a member for review in the first place. Distinct
        # from the branch above: that one is a hard restriction based on
        # role, this is an opt-in filter on top of the wider view.
        where_clauses.append("unrestriction_requests.requested_by_staff_id = ?")
        params.append(g.staff["id"])

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
    conn.close()

    context = {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "status_filter": status_filter or "",
        "requests_list": rows,
    }

    if request.headers.get("HX-Request") == "true":
        # staff=g.staff is required here (not just for the full-page load) --
        # the partial's own can_review check reads the bare `staff` variable,
        # which would otherwise be undefined on every htmx filter/pagination
        # swap and silently hide admin/in_charge review actions after the
        # first load. Same reasoning as staff_bp.view_staff.
        return render_template(
            "requests/_unrestriction_table.html", staff=g.staff, **context
        )

    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in rows],
        }
    )


# # # REVIEW (admin/in_charge only) ---------------------------------------


def _load_pending_request(id, conn):
    """Returns (request_row, error_response). error_response is None if
    the request exists and is still pending."""
    req = conn.execute(
        "SELECT * FROM unrestriction_requests WHERE id = ?", (id,)
    ).fetchone()

    if req is None:
        return None, (jsonify({"error": "Request not found"}), 404)

    if req["status"] != "pending":
        return None, (
            jsonify({"error": f"This request has already been {req['status']}"}),
            409,
        )

    return req, None


@requests_bp.route("/unrestriction-requests/<int:id>/approve", methods=["POST"])
@limiter.limit("20 per minute")
@roles_required("admin", "in_charge")
def approve_unrestriction_request(id):
    data, error = get_json_or_error()
    review_notes = data.get("review_notes") if not error else None

    conn = get_db_connection()
    req, error = _load_pending_request(id, conn)
    if error:
        conn.close()
        return error

    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (req["member_id"],)
    ).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member no longer exists"}), 404

    if g.staff["member_id"] == req["member_id"]:
        conn.close()
        return (
            jsonify(
                {
                    "error": "You cannot approve an unrestriction request for your own account.",
                    "reason": "Ask another admin or in_charge to review it.",
                }
            ),
            403,
        )

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated_name = full_name(
        member["first_name"],
        member["last_name"],
        member["middle_name"],
        member["suffix"],
    )

    try:
        # Same effect as the direct unrestrict_member route — approval
        # is just that action gated behind a second set of eyes.
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
            (g.staff["id"], review_notes, reviewed_at, id),
        )
        clear_unrestriction_pending_notifications(conn, id)
        create_unrestriction_resolved_notification(
            conn, id, req["requested_by_staff_id"], updated_name, approved=True
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="approved_unrestriction_request",
        target_table="unrestriction_requests",
        target_id=id,
        target_name=updated_name,
        details=f"Approved unrestriction for member {updated_name}",
    )

    return jsonify({"message": "Request approved, member unrestricted"})


@requests_bp.route("/unrestriction-requests/<int:id>/deny", methods=["POST"])
@limiter.limit("20 per minute")
@roles_required("admin", "in_charge")
def deny_unrestriction_request(id):
    data, error = get_json_or_error()
    if error:
        return error

    review_notes = data.get("review_notes")
    if not review_notes:
        return jsonify({"error": "Missing required field: review_notes"}), 400

    conn = get_db_connection()
    req, error = _load_pending_request(id, conn)
    if error:
        conn.close()
        return error

    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (req["member_id"],)
    ).fetchone()
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
            (g.staff["id"], review_notes, reviewed_at, id),
        )
        clear_unrestriction_pending_notifications(conn, id)
        create_unrestriction_resolved_notification(
            conn, id, req["requested_by_staff_id"], updated_name, approved=False, review_notes=review_notes
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="denied_unrestriction_request",
        target_table="unrestriction_requests",
        target_id=id,
        target_name=updated_name,
        details=f"Denied unrestriction request: {review_notes}",
    )

    return jsonify({"message": "Request denied"})
