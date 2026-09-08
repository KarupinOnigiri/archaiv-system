"""Query-building ug mutation logic para sa routes/monitoring.py (RFID
exit-monitoring events). `record_rfid_event` ang usa ka function nga
gitawag gawas sa normal nga browser request - ang Raspberry Pi's
background RFID listener (outside ani nga repo, naa sa tool-room
controller) mag-POST sa matag de-duplicated detection ngadto sa
routes/monitoring.py's record endpoint, nga mo-forward diretso ani.
"""

import logging

from flask import jsonify

from database import sqlite3
from services.settings_service import get_alarm_enabled
from utils.logger import log_action
from utils.validators import validate_date_string, escape_like_wildcards

logger = logging.getLogger(__name__)

CLASSIFICATIONS = ("authorized", "unauthorized", "unknown_tag")


def _no_ctx(error_payload, status):
    return (error_payload, status, {})


# # # LIVE ALARM STATE ------------------------------------------------------


def get_alarm_status(conn):
    """Ang usa ra ka row nga gina-poll sa alarm banner. 'Active' nagpasabot
    nga ang pinakabag-o nga unauthorized detection wala pa ma-clear sa
    Master Key Card tap - tuyo gyud nga walay software-only nga paagi
    mo-off ani, deactivate_active_alarms() ra ang mag-usab, ang tinuod nga
    HF/NFC deactivation event ra ang mag-tawag ani."""
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
    """I-classify ang usa ka de-duplicated nga detection event ug i-log.

    Classification rule: ang tag nga naa sa asset nga 'borrowed' karon
    authorized; ang tag nga naa sa asset apan laing status unauthorized;
    ang tag nga wala gyu'y match nga asset 'unknown_tag' - gilain gikan sa
    unauthorized para dili sila magsagol sa confusion-matrix data."""
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
    #
    # unknown_tag deliberately does NOT alarm - walay match sa inventory
    # ang tag (mahimong stray/foreign tag, staff badge, etc.), so there's
    # no confirmed asset actually leaving unauthorized. Still classified
    # and logged normally, just doesn't raise the alarm/relay.
    alarm_activated = classification == "unauthorized" and get_alarm_enabled(conn)

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
        conn=conn,
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
    """I-clear ang tanang active nga alarm dungan - usually usa ra ka
    active, apan gina-cover ang edge case sa duha ka detection nga
    ni-landing sa dili pa ma-process ang tap. Ang staff_id mao ang Admin
    nga na-identify sa Master Key Card, dili browser session - kini ra
    para sa Pi's HF/NFC handler, walay "deactivate" button sa UI."""
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
        conn=conn,
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
    """Mag-himo sa WHERE clause + params nga shared sa table listing -
    mirrors transaction_service.build_transactions_filters' shape."""
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
            "(assets.name LIKE ? ESCAPE '\\' OR assets.barcode_id LIKE ? ESCAPE '\\'"
            " OR members.first_name LIKE ? ESCAPE '\\' OR members.last_name LIKE ? ESCAPE '\\'"
            " OR members.barcode_id LIKE ? ESCAPE '\\')"
        )
        like = f"%{escape_like_wildcards(q)}%"
        params.extend([like, like, like, like, like])

    return where_clauses, params, classification_filter, None


def get_monitoring_table_context(conn, args, limit, offset):
    """Shared table context para sa monitoring_page() ug sa htmx table
    partial."""
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


def get_event_details(conn, event_id):
    """Full detail view sa usa ka detection event - parehas nga joins sa
    get_monitoring_table_context's row shape, single row ra ang gina-kuha
    para sa row-click details modal."""
    event = conn.execute(
        """
        SELECT rfid_events.*, assets.asset_id AS asset_code, assets.name AS asset_name,
               assets.barcode_id AS asset_barcode, assets.status AS asset_status,
               categories.name AS category_name,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id AS member_barcode,
               staff.username AS deactivated_by_username
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN categories ON categories.id = assets.category_id
        LEFT JOIN members ON members.id = rfid_events.member_id
        LEFT JOIN staff ON staff.id = rfid_events.deactivated_by
        WHERE rfid_events.id = ?
        """,
        (event_id,),
    ).fetchone()

    if event is None:
        return None, (jsonify({"error": "Detection event not found"}), 404)

    return event, None


def get_recent_events(conn, limit=5):
    """Pinakabag-o nga mga detection para sa Dashboard's Monitoring
    widget - pareho ka idea sa dashboard_service.py's recent_activity,
    naka-scope ra sa rfid_events imbes transactions."""
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
    """Today-scoped nga mga ihap para sa page header - mirrors
    get_asset_stat_cards's shape."""
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
