"""Query-building and mutation logic for routes/reservations.py - advance
booking of a specific serialized asset for a member, ahead of time.

Same convention as the other *_service modules: functions take an already-open
`conn` and return `(data, error)`, where `error` is `None` on success or a
ready `(jsonify(...), status)` tuple on failure. Structurally mirrors
services/unrestriction_service.py (a status-flow entity tied to an
asset/member/staff), with one deliberate difference: creation doesn't need
admin/in_charge review - a staff member booking a tool for a member is
self-approving, not something to be reviewed the way an unrestriction
request is.

CRITICAL DESIGN NOTE: assets.status='reserved' means EXCLUSIVELY "sitting in
an open checkout-session cart right now" - claimed via the atomic CAS in
services/transaction_service.py's add_item_to_checkout_session. A reservation
row here NEVER writes to assets.status. Availability for booking is decided
by checking for overlapping 'scheduled' rows in THIS table, not by asset
state.

V1 SCOPE: reservations are advisory, not enforced. An asset with an upcoming
reservation can still be checked out today by anyone - staff see the
"reserved for" banner (templates/assets/_details_modal.html) and use
judgment. This keeps the existing checkout CAS untouched and is the single
biggest risk-reduction decision in this feature; auto-linking a fulfilled
reservation to its actual checkout transaction (fulfilled_transaction_id)
is deferred to a later pass - the column exists for that future use but
nothing sets it yet.

Kini nga file ang naghandle sa advance booking (pag-reserve nang daan) sa
usa ka serialized nga asset para sa usa ka member. IMPORTANTE GYUD: ang
assets.status='reserved' NAGPASABOT RA "naa sa open checkout-session cart
karon dayon" - lahi kaayo sa reservation, ug ang reservation row DILI GYUD
mosuwat sa assets.status. V1 SCOPE: ang reservation ADVISORY ra, DILI
enforced - ang asset nga naay umaabot nga reservation pwede gihapon
ma-checkout karong adlawa sa bisan kinsa.
"""

import logging
from datetime import datetime

from flask import jsonify

from database import sqlite3
from services.notification_service import create_reservation_cancelled_notification
from utils.formatters import full_name
from utils.logger import log_action

logger = logging.getLogger(__name__)

VALID_STATUSES = ("scheduled", "fulfilled", "cancelled", "expired")


def create_reservation(conn, asset_id, member_id, start_at, end_at, staff, notes=None):
    """I-reserve ang serialized asset para sa member sulod sa [start_at,
    end_at) - susihon una nga tinuod ang asset, wala pa na-delete, dili
    consumable; tinuod ang member; balido ang window; ug dili nag-overlap
    sa laing 'scheduled' nga reservation sa parehas nga asset.

    Returns: tuple (result, error) - 404/400/409 kung napakyas."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, (jsonify({"error": "Asset not found"}), 404)
    if asset["deleted_at"] is not None:
        return None, (jsonify({"error": "Cannot reserve a deleted asset"}), 400)
    if asset["asset_type"] == "consumable":
        return None, (jsonify({"error": "Consumables can't be reserved - only serialized assets can"}), 400)

    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, (jsonify({"error": "Member not found"}), 404)

    if end_at <= start_at:
        return None, (jsonify({"error": "end_at must be after start_at"}), 400)

    overlap = conn.execute(
        """
        SELECT id FROM reservations
        WHERE asset_id = ? AND status = 'scheduled'
          AND NOT (end_at <= ? OR start_at >= ?)
        """,
        (asset_id, start_at, end_at),
    ).fetchone()
    if overlap is not None:
        return None, (
            jsonify({"error": f"{asset['name']} already has an overlapping reservation", "reservation_id": overlap["id"]}),
            409,
        )

    try:
        cursor = conn.execute(
            """
            INSERT INTO reservations (asset_id, member_id, staff_id, start_at, end_at, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (asset_id, member_id, staff["id"], start_at, end_at, notes),
        )
        reservation_id = cursor.lastrowid
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, (jsonify({"error": "A database constraint was violated."}), 400)

    member_name = full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"])
    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="created_reservation",
        target_table="reservations",
        target_id=reservation_id,
        target_name=f"{asset['asset_id']} - {asset['name']} for {member_name}",
        details=f"Reserved {asset['asset_id']} for {member_name}, {start_at} to {end_at}",
    )

    return {"message": "Reservation created", "id": reservation_id}, None


def _load_scheduled_reservation(conn, reservation_id):
    """Mobalik ug (reservation_row, error_response) - None ang error kung
    naa gyud ang reservation ug 'scheduled' pa gihapon. 404 kung wala
    makit-i, 409 kung dili na 'scheduled'."""
    res = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    if res is None:
        return None, (jsonify({"error": "Reservation not found"}), 404)
    if res["status"] != "scheduled":
        return None, (jsonify({"error": f"This reservation is already {res['status']}"}), 409)
    return res, None


def cancel_reservation(conn, reservation_id, staff):
    """Bisan kinsa pwede mo-cancel sa iyang kaugalingong booking; ang
    admin/in_charge pwede pod mo-cancel sa booking sa lain - apan ang
    tool_keeper DILI pwede mo-cancel sa booking sa laing staff.

    Returns: tuple (result, error) - 404/409/403 kung napakyas."""
    res, error = _load_scheduled_reservation(conn, reservation_id)
    if error:
        return None, error

    if staff["id"] != res["staff_id"] and staff["role"] not in ("admin", "in_charge"):
        return None, (jsonify({"error": "You can only cancel your own reservations"}), 403)

    asset = conn.execute("SELECT asset_id, name FROM assets WHERE id = ?", (res["asset_id"],)).fetchone()
    member = conn.execute(
        "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?", (res["member_id"],)
    ).fetchone()
    member_name = full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"]) if member else "the member"

    try:
        conn.execute(
            """
            UPDATE reservations
            SET status = 'cancelled', cancelled_at = datetime('now', 'localtime'), cancelled_by_staff_id = ?
            WHERE id = ?
            """,
            (staff["id"], reservation_id),
        )
        # Only notify the original booking staff member if someone else cancelled it -
        # cancelling your own booking needs no notice.
        if res["staff_id"] != staff["id"] and asset is not None:
            create_reservation_cancelled_notification(conn, reservation_id, res["staff_id"], asset["name"], member_name)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="cancelled_reservation",
        target_table="reservations",
        target_id=reservation_id,
        target_name=f"{asset['asset_id']} - {asset['name']}" if asset else None,
        details=f"Cancelled reservation #{reservation_id}",
    )

    return {"message": "Reservation cancelled"}, None


def get_reservations_context(conn, args, limit, offset, staff):
    """Ang admin/in_charge makakita sa TANANG reservation; ang tool_keeper
    makakita ra sa iyang kaugalingong gi-book.

    Returns: tuple (context, error)."""
    status_filter = args.get("status")
    if status_filter and status_filter not in VALID_STATUSES:
        return None, (jsonify({"error": f"status must be one of: {', '.join(VALID_STATUSES)}"}), 400)

    where_clauses = []
    params = []

    if staff["role"] not in ("admin", "in_charge"):
        where_clauses.append("reservations.staff_id = ?")
        params.append(staff["id"])
    elif args.get("mine") == "1":
        where_clauses.append("reservations.staff_id = ?")
        params.append(staff["id"])

    if status_filter:
        where_clauses.append("reservations.status = ?")
        params.append(status_filter)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM reservations {where_sql}", params
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT reservations.*,
               assets.asset_id AS asset_code, assets.name AS asset_name,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               booked_by.username AS staff_username,
               cancelled_by.username AS cancelled_by_username
        FROM reservations
        JOIN assets ON reservations.asset_id = assets.id
        JOIN members ON reservations.member_id = members.id
        JOIN staff AS booked_by ON reservations.staff_id = booked_by.id
        LEFT JOIN staff AS cancelled_by ON reservations.cancelled_by_staff_id = cancelled_by.id
        {where_sql}
        ORDER BY reservations.start_at DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "status_filter": status_filter or "",
        "reservations_list": rows,
    }, None


def get_upcoming_reservations_for_asset(conn, asset_id, limit=5):
    """Gigamit sa "RESERVED FOR" banner sa details modal - ang mga
    reservation ra nga importante gihapon ('scheduled', wala pa
    ma-tapos), sunod sa han-ay gikan sa pinakasunod.

    Returns: list[dict]."""
    rows = conn.execute(
        """
        SELECT reservations.*,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM reservations
        JOIN members ON reservations.member_id = members.id
        WHERE reservations.asset_id = ? AND reservations.status = 'scheduled'
          AND reservations.end_at >= datetime('now', 'localtime')
        ORDER BY reservations.start_at
        LIMIT ?
        """,
        (asset_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def sweep_expired_reservations():
    """I-usab ang mga 'scheduled' nga row nga nangagi na sa 'expired' -
    naay kaugalingong koneksyon, naa sa iskedyul (app.py)."""
    from database import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "UPDATE reservations SET status = 'expired' WHERE status = 'scheduled' AND end_at < datetime('now', 'localtime')"
        )
        conn.commit()
        if cursor.rowcount:
            logger.info(f"Expired {cursor.rowcount} past-due reservation(s)")
    except Exception:
        conn.rollback()
        logger.exception("Failed to sweep expired reservations")
    finally:
        conn.close()
