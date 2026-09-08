"""Query-building ug mutation logic para sa routes/reservations.py -
advance booking sa usa ka specific serialized asset para sa usa ka
member, nang daan pa.

Same convention sa ubang *_service modules: matag function mo-dawat ug
bukas nga `conn`, mobalik ug `(data, error)` - `error` None kung
malampuson, o ready `(jsonify(...), status)` tuple kung napakyas.
Structurally mirrors services/unrestriction_service.py (status-flow entity
nga naka-tie sa asset/member/staff), pero lahi sa usa ka butang: ang
creation dili na kinahanglan admin/in_charge review - kung mag-book ug
tool ang staff para sa member, self-approving na dayon, dili parehas sa
unrestriction request nga kinahanglan pa i-review.

IMPORTANTE KAAYO: ang assets.status='reserved' EXCLUSIVE ra sa "naa sa
open checkout-session cart karon dayon" - gina-claim via atomic CAS sa
services/transaction_service.py's add_item_to_checkout_session. Ang
reservation row dinhi DILI GYUD mosuwat sa assets.status. Availability
para sa booking gina-check base sa overlapping 'scheduled' rows sa KINING
table ra, dili base sa asset's own status.

V1 SCOPE: advisory ra ang reservations, dili enforced. Ang asset nga naay
umaabot nga reservation pwede gihapon ma-checkout karong adlawa sa bisan
kinsa - ang staff makakita ra sa "reserved for" banner
(templates/assets/_details_modal.html) ug gamiton ang judgment. Mao ni
ang pinakadako nga risk-reduction decision ani nga feature (wala gi-touch
ang existing checkout CAS); ang pag-auto-link sa fulfilled reservation
ngadto sa iyang actual checkout transaction (fulfilled_transaction_id)
deferred pa sa later pass - naa na ang column para ana pero wala pay
nagsuwat niini.
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

    # The SELECT above is just a fast-path/friendly-message check - it holds
    # no lock, so two staff booking the same window at the same moment could
    # both pass it (TOCTOU). Closing that: INSERT first (SQLite's WAL mode
    # serializes writers - this statement now holds the write lock until
    # commit/rollback, so a concurrent create_reservation() blocks here up to
    # busy_timeout instead of racing past), then re-check for an overlap a
    # SECOND time excluding this new row's own id. If a concurrent request
    # beat us to it and already committed, we now see it and roll back
    # instead of leaving two overlapping bookings on the same asset.
    try:
        cursor = conn.execute(
            """
            INSERT INTO reservations (asset_id, member_id, staff_id, start_at, end_at, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (asset_id, member_id, staff["id"], start_at, end_at, notes),
        )
        reservation_id = cursor.lastrowid

        overlap = conn.execute(
            """
            SELECT id FROM reservations
            WHERE asset_id = ? AND status = 'scheduled' AND id != ?
              AND NOT (end_at <= ? OR start_at >= ?)
            """,
            (asset_id, reservation_id, start_at, end_at),
        ).fetchone()
        if overlap is not None:
            conn.rollback()
            return None, (
                jsonify(
                    {"error": f"{asset['name']} already has an overlapping reservation", "reservation_id": overlap["id"]}
                ),
                409,
            )

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


def search_reservable_assets(conn, q, limit=30):
    """Asset picker para sa Reservations page's "New Reservation" button
    (dili pareha sa Details modal's "Reserve", nga naa nay pre-selected
    asset na - kani nagsugod nga walay asset pa, so kinahanglan naay
    picker una). Nagtugot ra og non-consumable, dili pa deleted nga
    assets, mo-match sa name/asset_id/barcode_id. Blangko nga `q` mobalik
    og pinakabag-o nga assets (browse-all case, parehas sa checkout's
    "Show available members").

    Returns: list[dict] {id, asset_id, name, category, status}."""
    q = (q or "").strip()
    if q:
        rows = conn.execute(
            """
            SELECT assets.id, assets.asset_id, assets.name, assets.status,
                   categories.name AS category
            FROM assets
            LEFT JOIN categories ON categories.id = assets.category_id
            WHERE assets.deleted_at IS NULL AND assets.asset_type != 'consumable'
              AND (
                  assets.name LIKE '%' || ? || '%'
                  OR UPPER(assets.asset_id) LIKE '%' || UPPER(?) || '%'
                  OR UPPER(assets.barcode_id) LIKE '%' || UPPER(?) || '%'
              )
            ORDER BY assets.name
            LIMIT ?
            """,
            (q, q, q, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT assets.id, assets.asset_id, assets.name, assets.status,
                   categories.name AS category
            FROM assets
            LEFT JOIN categories ON categories.id = assets.category_id
            WHERE assets.deleted_at IS NULL AND assets.asset_type != 'consumable'
            ORDER BY assets.name
            LIMIT ?
            """,
            (limit,),
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
