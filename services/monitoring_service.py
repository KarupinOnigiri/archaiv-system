"""Query-building and mutation logic for routes/monitoring.py (RFID exit-monitoring events).

Same (data, error) convention as the other services - see asset_service.py's module
docstring. `record_rfid_event` is the one function meant to be called from outside a
normal browser request: the Raspberry Pi's background RFID listener (not part of this
repo - it runs on the tool-room controller, see the thesis's Chapter 3 hardware
description) is expected to POST each de-duplicated detection to
routes/monitoring.py's record endpoint, which just forwards into this module.
"""

import logging

from flask import jsonify

from database import sqlite3
from services.settings_service import get_alarm_enabled
from utils.logger import log_action
from utils.validators import validate_date_string

logger = logging.getLogger(__name__)

CLASSIFICATIONS = ("authorized", "unauthorized", "unknown_tag")


def _no_ctx(error_payload, status):
    return (error_payload, status, {})


# # # LIVE ALARM STATE ------------------------------------------------------


def get_alarm_status(conn):
    """The single row the alarm banner polls. 'Active' means the most recent
    unauthorized detection hasn't been cleared by a Master Key Card tap yet --
    there is deliberately no software-only way to flip this off (see the
    migration's comment); it only changes via deactivate_active_alarms(),
    which the physical HF/NFC deactivation event is expected to call."""
    active_event = conn.execute(
        """
        SELECT rfid_events.*, assets.asset_id AS asset_code, assets.name AS asset_name,
               assets.barcode_id AS asset_barcode
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        WHERE rfid_events.alarm_activated = 1 AND rfid_events.alarm_deactivated_at IS NULL
        ORDER BY rfid_events.detected_at DESC
        LIMIT 1
        """
    ).fetchone()

    active_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM rfid_events WHERE alarm_activated = 1 AND alarm_deactivated_at IS NULL"
    ).fetchone()["cnt"]

    return {
        "active": active_count > 0,
        "active_count": active_count,
        "event": dict(active_event) if active_event else None,
    }


# # # RECORD (called by the Pi's RFID listener, via routes/monitoring.py) --


def record_rfid_event(conn, rfid_tag_scanned, staff_id=None):
    """Classifies one de-duplicated detection event and logs it.

    Classification rule (mirrors the thesis's alarm logic): a tag that
    matches a tool currently checked out ('borrowed') is authorized for
    exit; a tag that matches a known tool in any other status is
    unauthorized; a tag that matches no asset at all is 'unknown_tag' --
    kept distinct from 'unauthorized' so a stray/mis-tagged item doesn't
    get counted the same as a real unauthorized-removal event in the
    confusion-matrix data this table exists to support.
    """
    rfid_tag_scanned = (rfid_tag_scanned or "").strip()
    if not rfid_tag_scanned:
        return None, _no_ctx({"error": "Missing required field: rfid_tag_scanned"}, 400)

    asset = conn.execute(
        "SELECT * FROM assets WHERE UPPER(rfid_id) = UPPER(?) AND deleted_at IS NULL",
        (rfid_tag_scanned,),
    ).fetchone()

    member_id = None
    if asset is None:
        classification = "unknown_tag"
        asset_id = None
    elif asset["status"] == "borrowed":
        classification = "authorized"
        asset_id = asset["id"]
        # Captured now, not looked up live when the table is later viewed --
        # this loan's transaction row can (and often will) flip to 'returned'
        # before anyone looks at the log, at which point a live "who has this
        # borrowed" query would come back empty. See migration 0049.
        active_loan = conn.execute(
            "SELECT member_id FROM transactions WHERE asset_id = ? AND status = 'borrowed'",
            (asset_id,),
        ).fetchone()
        member_id = active_loan["member_id"] if active_loan else None
    else:
        classification = "unauthorized"
        asset_id = asset["id"]

    # The alarm itself can be muted independently of the whole Monitoring
    # feature (feature_flags.monitoring gates the tab/pipeline entirely) -
    # detections still get classified and logged exactly the same either
    # way, this only decides whether that classification actually raises
    # the banner/active-alarm state.
    alarm_activated = classification in ("unauthorized", "unknown_tag") and get_alarm_enabled(conn)

    try:
        cursor = conn.execute(
            """
            INSERT INTO rfid_events (rfid_tag_scanned, asset_id, classification, alarm_activated, member_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (rfid_tag_scanned, asset_id, classification, int(alarm_activated), member_id),
        )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Failed to record RFID event: {e}")
        return None, _no_ctx({"error": "Failed to record detection event"}, 500)

    new_id = cursor.lastrowid

    log_action(
        staff_id=staff_id,
        action="rfid_event_recorded",
        target_table="rfid_events",
        target_id=new_id,
        target_name=asset["asset_id"] if asset else rfid_tag_scanned,
        details=f"Tag '{rfid_tag_scanned}' detected at monitored exit - classified {classification}"
        + (" - alarm activated" if alarm_activated else ""),
    )

    return {
        "id": new_id,
        "classification": classification,
        "alarm_activated": alarm_activated,
        "asset_id": asset_id,
    }, None


# # # DEACTIVATE (physical Master Key Card tap, reported by the Pi) --------


def deactivate_active_alarms(conn, staff_id):
    """Clears every currently-active alarm at once (there should only ever be
    one active at a time in practice, but this covers the edge case of two
    detections landing before the tap is processed). staff_id is the Admin
    identified by the Master Key Card, not a browser session - the route
    calling this on the software side is meant only for the Pi's HF/NFC
    handler to invoke, never a plain "deactivate" button in the UI."""
    active_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM rfid_events WHERE alarm_activated = 1 AND alarm_deactivated_at IS NULL"
        ).fetchall()
    ]

    if not active_ids:
        return {"deactivated_count": 0}, None

    try:
        conn.executemany(
            """
            UPDATE rfid_events
            SET alarm_deactivated_at = datetime('now', 'localtime'), deactivated_by = ?
            WHERE id = ?
            """,
            [(staff_id, event_id) for event_id in active_ids],
        )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Failed to deactivate RFID alarm(s): {e}")
        return None, _no_ctx({"error": "Failed to deactivate alarm"}, 500)

    log_action(
        staff_id=staff_id,
        action="rfid_alarm_deactivated",
        target_table="rfid_events",
        details=f"Deactivated {len(active_ids)} active alarm event(s) via Master Key Card",
    )

    return {"deactivated_count": len(active_ids)}, None


# # # LIST / TABLE ----------------------------------------------------------


MONITORING_SORTABLE_COLUMNS = {
    "detected_at": "rfid_events.detected_at",
    "classification": "rfid_events.classification",
    "asset_name": "assets.name",
    "category": "categories.name",
    "barcode": "assets.barcode_id",
    "member_name": "members.last_name",
}


def build_monitoring_filters(args):
    """Builds the WHERE clause + params shared by the table listing - mirrors
    transaction_service.build_transactions_filters' shape."""
    where_clauses = []
    params = []

    classification_filter = args.get("classification")
    if classification_filter:
        if classification_filter not in CLASSIFICATIONS:
            return None, None, None, (
                jsonify({"error": f"classification must be one of: {', '.join(CLASSIFICATIONS)}"}),
                400,
            )
        where_clauses.append("rfid_events.classification = ?")
        params.append(classification_filter)

    since = args.get("since")
    if since:
        error = validate_date_string(since, "since")
        if error:
            return None, None, None, error
        where_clauses.append("date(rfid_events.detected_at) >= ?")
        params.append(since)

    until = args.get("until")
    if until:
        error = validate_date_string(until, "until")
        if error:
            return None, None, None, error
        where_clauses.append("date(rfid_events.detected_at) <= ?")
        params.append(until)

    # Searches the asset's name/barcode and the borrower's name/barcode --
    # not the raw scanned tag (that's what the TAG column is for, and an
    # unregistered/mismatched tag wouldn't match a real asset's identifiers
    # anyway) and not the borrower fields for events with no member_id
    # (unauthorized/unknown_tag - there's nothing to match there).
    q = args.get("q")
    if q:
        where_clauses.append(
            "(assets.name LIKE ? OR assets.barcode_id LIKE ?"
            " OR members.first_name LIKE ? OR members.last_name LIKE ? OR members.barcode_id LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])

    return where_clauses, params, classification_filter, None


def get_monitoring_table_context(conn, args, limit, offset):
    """Shared table context for monitoring_page() and the htmx table partial."""
    where_clauses, params, classification_filter, error = build_monitoring_filters(args)
    if error:
        return None, error

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    sort_by = args.get("sort_by", "detected_at")
    if sort_by not in MONITORING_SORTABLE_COLUMNS:
        return None, (
            jsonify({"error": f"sort_by must be one of: {', '.join(MONITORING_SORTABLE_COLUMNS)}"}),
            400,
        )
    sort_dir = args.get("sort_dir", "desc").lower()
    if sort_dir not in ("asc", "desc"):
        return None, (jsonify({"error": "sort_dir must be 'asc' or 'desc'"}), 400)
    sort_column = MONITORING_SORTABLE_COLUMNS[sort_by]

    # LEFT JOINs throughout - asset_id/member_id are both nullable (no match,
    # or an unauthorized/unknown event with no borrower to attribute).
    from_sql = """
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN categories ON categories.id = assets.category_id
        LEFT JOIN members ON members.id = rfid_events.member_id
        LEFT JOIN staff ON staff.id = rfid_events.deactivated_by
    """

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt {from_sql} {where_sql}", params
    ).fetchone()["cnt"]

    events = conn.execute(
        f"""
        SELECT rfid_events.*, assets.asset_id AS asset_code, assets.name AS asset_name,
               assets.barcode_id AS asset_barcode, categories.name AS category_name,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id AS member_barcode,
               staff.username AS deactivated_by_username
        {from_sql}
        {where_sql}
        ORDER BY {sort_column} {sort_dir}, rfid_events.id {sort_dir}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return {
        "events": events,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "classification": classification_filter or "",
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "q": args.get("q", ""),
        "since": args.get("since", ""),
        "until": args.get("until", ""),
    }, None


def get_recent_events(conn, limit=5):
    """Most recent detections for the Dashboard's Monitoring widget - a glance
    feed, same idea as dashboard_service.py's own recent_activity, just scoped
    to rfid_events instead of transactions."""
    rows = conn.execute(
        """
        SELECT rfid_events.id, rfid_events.detected_at, rfid_events.classification,
               assets.name AS asset_name, assets.asset_id AS asset_code
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        ORDER BY rfid_events.detected_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_monitoring_stat_cards(conn):
    """Today-scoped counts for the page header - mirrors get_asset_stat_cards's shape."""
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS total_count,
            SUM(CASE WHEN classification = 'authorized' THEN 1 ELSE 0 END) AS authorized_count,
            SUM(CASE WHEN classification = 'unauthorized' THEN 1 ELSE 0 END) AS unauthorized_count,
            SUM(CASE WHEN classification = 'unknown_tag' THEN 1 ELSE 0 END) AS unknown_count
        FROM rfid_events
        WHERE date(detected_at) = date('now', 'localtime')
        """
    ).fetchone()
    return {
        "stat_total": counts["total_count"] or 0,
        "stat_authorized": counts["authorized_count"] or 0,
        "stat_unauthorized": counts["unauthorized_count"] or 0,
        "stat_unknown": counts["unknown_count"] or 0,
    }
