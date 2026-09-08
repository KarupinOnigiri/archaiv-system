"""Query-building, checkout-session/loan business logic, ug export shaping para sa
routes/transactions.py. Matag function mo-dawat ug bukas nga `conn` (ang route ang
mo-open/close) ug mobalik ug `(data, error)` - `error` kay ready `(jsonify(...), status)`
tuple kung napakyas, `data` kay plain dict/list/row kung malampuson - para mag-stay ang
routes as thin parse-args -> call service -> jsonify layer, parehas sa utils/validators.py's
convention.
"""

import csv
import io
from datetime import datetime, timedelta

from flask import jsonify

from database import sqlite3
from services.loan_service import (
    check_and_restrict_overdue_member,
    check_and_lift_restriction_if_resolved,
    check_and_restrict_damaged_returns,
)
from services.settings_service import get_checkout_session_stale_minutes, is_feature_enabled
from services.reservation_service import get_upcoming_reservations_for_asset
from utils.formatters import full_name
from utils.logger import log_action
from utils.validators import validate_date_string, decimal_scan_hex_suffix

# Fallback only - real default lives in app_settings (settings_service.get_default_loan_days)
DEFAULT_LOAN_DAYS = 2
# Fallback only - real caps live in the loan_limits table (see get_max_concurrent_loans below)
MAX_CONCURRENT_LOANS = 10
MAX_AVAILABLE_ASSETS_LISTED = 300
MAX_AVAILABLE_MEMBERS_LISTED = 300
MAX_BATCH_RETURN_SIZE = 50


def _verify_second_scan(asset, verify_code):
    """Cross-check sa second (confirmatory) scan batok sa asset - mao ni ang
    actual anti-swap check (ang barcode sticker ra lang, mo-prove ra na *a*
    barcode ang gi-scan, ug ang sticker pwede man peelon ug ibutang sa laing
    tool). Match against sa asset's RFID tag, barcode, o iyang own asset ID
    code - dili tanan asset naay RFID tag na registered, ug bisan asa sa
    tulo ang gi-present balik sa staff, deliberate proof gihapon nga naa
    gyud sila sa tool mismo, dili lang re-type sa parehas value gikan sa
    memory - mao nga dili ni RFID-only bisan pa sa field name sa API
    payload (rfid_code, gi-keep ra para backward compatibility).

    Optional by design: `verify_code` kay None kung single scan ra ang
    staff nga wala'y follow-up verification, kinahanglan mo-work gihapon
    na siya normally. Mo-return ra ug error kung naa gyud gi-scan/i-type
    ang staff nga wa mo-match sa bisan unsa sa asset's identifiers."""
    if not verify_code:
        return None
    code = verify_code.strip().upper()
    candidates = {v.upper() for v in (asset["rfid_id"], asset["barcode_id"], asset["asset_id"]) if v}
    if code in candidates:
        return None
    # Some readers set to type scans as keyboard input only type the decimal
    # value of an EPC's last 3 bytes, not the full hex ID - see
    # decimal_scan_hex_suffix()'s docstring.
    hex_suffix = decimal_scan_hex_suffix(code)
    if hex_suffix and asset["rfid_id"] and asset["rfid_id"].upper().endswith(hex_suffix):
        return None
    return (
        jsonify(
            {
                "error": "That doesn't match this asset's barcode, ID, or RFID tag - it may not be the tool that was borrowed.",
                "code": "verification_mismatch",
            }
        ),
        409,
    )


VALID_RETURN_CONDITIONS = ("functional", "inoperable", "for_repair", "disposed")


def get_max_concurrent_loans(member_type, conn):
    """Mangita sa concurrent-loan cap para sa member_type - fallback sa
    MAX_CONCURRENT_LOANS kung wala'y row para ana sa loan_limits table."""
    row = conn.execute(
        "SELECT max_concurrent_loans FROM loan_limits WHERE member_type = ?",
        (member_type,),
    ).fetchone()
    return row["max_concurrent_loans"] if row else MAX_CONCURRENT_LOANS


def get_active_checkout_session(session_id, member_id, conn):
    """Mobalik ug (session_row, error) para sa open checkout session - lazily
    ma-close ang stale ones (last activity beyond the configured timeout)."""
    session = conn.execute(
        "SELECT * FROM checkout_sessions WHERE id = ?", (session_id,)
    ).fetchone()

    if session is None:
        return None, (
            jsonify(
                {"error": "session_id does not match an existing checkout session"}
            ),
            404,
        )

    if session["member_id"] != member_id:
        return None, (
            jsonify({"error": "session_id belongs to a different member"}),
            400,
        )

    if session["status"] != "open":
        return None, (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    last_activity_at = datetime.strptime(
        session["last_activity_at"] or session["created_at"], "%Y-%m-%d %H:%M:%S"
    )
    if datetime.now() - last_activity_at > timedelta(minutes=get_checkout_session_stale_minutes(conn)):
        # lazily close it so it stops showing up as "open"
        conn.execute(
            "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ? AND status = 'open'",
            (session_id,),
        )
        conn.commit()
        return None, (
            jsonify({"error": "This checkout session has timed out. Start a new one."}),
            409,
        )

    return session, None


def require_session_owner(session, staff_id):
    """Mo-error kung iya ni ug laing staff member - kung wa'y owner naka-record,
    accessible ra gihapon."""
    if session["staff_id"] is not None and session["staff_id"] != staff_id:
        return jsonify({"error": "This checkout session belongs to another staff member"}), 403
    return None


def load_open_sessions_for_staff(staff_id, conn):
    """Tanang open checkout sessions sa usa ka staff member, dungan sa items -
    single source para sa dock/banner (walay laing duplicate query)."""
    sessions = conn.execute(
        """
        SELECT checkout_sessions.*, members.barcode_id AS member_barcode, members.first_name AS member_first_name
        FROM checkout_sessions
        JOIN members ON checkout_sessions.member_id = members.id
        WHERE checkout_sessions.staff_id = ? AND checkout_sessions.status = 'open'
        ORDER BY checkout_sessions.created_at DESC
        """,
        (staff_id,),
    ).fetchall()

    stale_minutes = get_checkout_session_stale_minutes(conn)
    results = []
    for session in sessions:
        last_activity_at = datetime.strptime(
            session["last_activity_at"] or session["created_at"], "%Y-%m-%d %H:%M:%S"
        )
        if datetime.now() - last_activity_at > timedelta(minutes=stale_minutes):
            conn.execute(
                "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ? AND status = 'open'",
                (session["id"],),
            )
            conn.commit()
            continue

        items = conn.execute(
            """
            SELECT checkout_session_items.id AS id, checkout_session_items.asset_id AS asset_id, assets.name AS asset_name, assets.barcode_id AS barcode_id
            FROM checkout_session_items
            JOIN assets ON checkout_session_items.asset_id = assets.id
            WHERE checkout_session_items.checkout_session_id = ?
            ORDER BY checkout_session_items.added_at
            """,
            (session["id"],),
        ).fetchall()

        # Same advisory heads-up as add_item_to_checkout_session's
        # reservation_notice, recomputed here so it still shows up for an
        # item that was already in the cart before this session got
        # minimized/resumed - not just the moment it's first scanned.
        cart_items = [dict(row) for row in items]
        if cart_items and is_feature_enabled("reservations", conn):
            for cart_item in cart_items:
                upcoming = get_upcoming_reservations_for_asset(conn, cart_item["asset_id"], limit=1)
                if upcoming:
                    r = upcoming[0]
                    cart_item["reservation_notice"] = {
                        "member_name": full_name(r["first_name"], r["last_name"], r["middle_name"], r["suffix"]),
                        "start_at": r["start_at"],
                        "end_at": r["end_at"],
                    }
                else:
                    cart_item["reservation_notice"] = None
        else:
            for cart_item in cart_items:
                cart_item["reservation_notice"] = None

        results.append({**dict(session), "cart_items": cart_items})

    return results


def load_open_session_for_staff(staff_id, conn):
    """Ang pinaka-recent open session, o None - thin wrapper para sa callers
    nga usa ra ilang gusto."""
    sessions = load_open_sessions_for_staff(staff_id, conn)
    return sessions[0] if sessions else None


# # # LIST / VIEW ------------------------------------------------------------


def list_transactions(conn, limit, offset):
    """Paginated nga tanang transactions, newest time_out una, dungan sa asset
    + member names para dili na kinahanglan mag-separate lookup."""
    total_count = conn.execute("SELECT COUNT(*) AS cnt FROM transactions").fetchone()["cnt"]
    transactions = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        ORDER BY transactions.time_out DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "results": [dict(row) for row in transactions],
    }


def list_overdue_transactions(conn):
    """Tanang borrowed transactions nga lapas na sa due_date - sorted soonest
    overdue una."""
    overdue = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.status = 'borrowed'
        AND transactions.due_date IS NOT NULL
        AND transactions.due_date < datetime('now', 'localtime')
        ORDER BY transactions.due_date ASC
        """
    ).fetchall()
    return [dict(row) for row in overdue]


# # # LIST / VIEW (htmx table partial) --------------------------------------

TRANSACTIONS_SORTABLE_COLUMNS = {
    "time_out": "transactions.time_out",
    "due_date": "transactions.due_date",
    "time_in": "transactions.time_in",
    "status": "transactions.status",
    "asset_name": "assets.name",
    "member_name": "members.last_name",
}

TRANSACTIONS_EXPORT_COLUMNS = [
    ("asset_code", "Asset Code"),
    ("asset_name", "Asset"),
    ("member_name", "Member"),
    ("time_out", "Time Out"),
    ("due_date", "Due Date"),
    ("time_in", "Return Date"),
    ("status", "Status"),
    ("condition_out", "Condition Out"),
    ("condition_in", "Condition In"),
    ("borrowed_by_username", "Checked Out By"),
    ("returned_by_username", "Returned To"),
    ("return_remarks", "Remarks"),
]


def build_transactions_filters(args):
    """Mag-build sa WHERE clause + params nga gi-share sa table listing ug CSV
    export, para dili na duplicate ang filter logic."""
    where_clauses = []
    params = []

    # member_id is an exact-match filter for deep-links, avoiding name-collision false matches.
    member_id = args.get("member_id")
    if member_id:
        try:
            member_id = int(member_id)
        except (TypeError, ValueError):
            return None, None, None, (
                jsonify({"error": "member_id must be an integer"}),
                400,
            )
        where_clauses.append("transactions.member_id = ?")
        params.append(member_id)

    q = args.get("q")
    if q:
        where_clauses.append(
            "(assets.name LIKE ? OR assets.asset_id LIKE ? OR members.first_name LIKE ?"
            " OR members.last_name LIKE ? OR members.barcode_id LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])

    # "overdue" is a virtual status (borrowed + past due); keep in sync with is_overdue below.
    status_filter = args.get("status")
    if status_filter == "overdue":
        where_clauses.append(
            "transactions.status = 'borrowed' AND transactions.due_date IS NOT NULL AND transactions.due_date < datetime('now', 'localtime')"
        )
    elif status_filter in ("borrowed", "returned"):
        where_clauses.append("transactions.status = ?")
        params.append(status_filter)

    time_out_from = args.get("time_out_from")
    if time_out_from:
        error = validate_date_string(time_out_from, "time_out_from")
        if error:
            return None, None, None, error
        where_clauses.append("date(transactions.time_out) >= ?")
        params.append(time_out_from)

    time_out_to = args.get("time_out_to")
    if time_out_to:
        error = validate_date_string(time_out_to, "time_out_to")
        if error:
            return None, None, None, error
        where_clauses.append("date(transactions.time_out) <= ?")
        params.append(time_out_to)

    return where_clauses, params, status_filter, None


def get_transactions_table_context(conn, args, limit, offset):
    """Shared table context para sa view_transactions_table() ug
    transactions_page(), para magkasabot sila permi (sort/filter/pagination
    tanan dinhi lang gi-resolve)."""
    where_clauses, params, status_filter, error = build_transactions_filters(args)
    if error:
        return None, error

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    sort_by = args.get("sort_by", "time_out")
    if sort_by not in TRANSACTIONS_SORTABLE_COLUMNS:
        return None, (
            jsonify(
                {"error": f"sort_by must be one of: {', '.join(TRANSACTIONS_SORTABLE_COLUMNS)}"}
            ),
            400,
        )
    sort_dir = args.get("sort_dir", "desc").lower()
    if sort_dir not in ("asc", "desc"):
        return None, (jsonify({"error": "sort_dir must be 'asc' or 'desc'"}), 400)
    sort_column = TRANSACTIONS_SORTABLE_COLUMNS[sort_by]

    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        {where_sql}
        """,
        params,
    ).fetchone()["cnt"]

    transactions = conn.execute(
        f"""
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               borrowed_staff.username AS borrowed_by_username,
               returned_staff.username AS returned_by_username,
               CASE WHEN transactions.status = 'borrowed'
                    AND transactions.due_date IS NOT NULL
                    AND transactions.due_date < datetime('now', 'localtime')
               THEN 1 ELSE 0 END AS is_overdue
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        LEFT JOIN staff AS borrowed_staff ON transactions.borrowed_by_staff_id = borrowed_staff.id
        LEFT JOIN staff AS returned_staff ON transactions.returned_by_staff_id = returned_staff.id
        {where_sql}
        ORDER BY {sort_column} {sort_dir.upper()}, transactions.id {sort_dir.upper()}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return {
        "transactions": transactions,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "status_filter": status_filter or "",
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }, None


def build_transactions_export_csv(conn, args):
    """CSV bytes sa current filtered table - gi-ignore ang sort/pagination
    kay export man gud, tibuok set gyud kinahanglan."""
    where_clauses, params, _status_filter, error = build_transactions_filters(args)
    if error:
        return None, error
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    rows = conn.execute(
        f"""
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               borrowed_staff.username AS borrowed_by_username,
               returned_staff.username AS returned_by_username
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        LEFT JOIN staff AS borrowed_staff ON transactions.borrowed_by_staff_id = borrowed_staff.id
        LEFT JOIN staff AS returned_staff ON transactions.returned_by_staff_id = returned_staff.id
        {where_sql}
        ORDER BY transactions.time_out DESC
        """,
        params,
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in TRANSACTIONS_EXPORT_COLUMNS])
    for row in rows:
        record = dict(row)
        record["member_name"] = full_name(
            row["first_name"], row["last_name"], row["middle_name"], row["suffix"]
        )
        writer.writerow(
            [record.get(col) if record.get(col) is not None else "" for col, _ in TRANSACTIONS_EXPORT_COLUMNS]
        )
    # BOM so Excel handles accented characters right on direct double-click open
    return output.getvalue().encode("utf-8-sig"), None


def get_transaction_stat_cards(conn):
    """Ang 4 ka stat-card numbers (All/Borrowed/Overdue/Returned), parehas ra
    ka pattern sa /assets/stat-cards."""
    stat_all = conn.execute("SELECT COUNT(*) AS cnt FROM transactions").fetchone()["cnt"]
    stat_borrowed = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE status = 'borrowed'"
    ).fetchone()["cnt"]
    stat_overdue = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM transactions
        WHERE status = 'borrowed' AND due_date IS NOT NULL AND due_date < datetime('now', 'localtime')
        """
    ).fetchone()["cnt"]
    stat_returned = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE status = 'returned'"
    ).fetchone()["cnt"]
    return {
        "stat_all": stat_all,
        "stat_borrowed": stat_borrowed,
        "stat_overdue": stat_overdue,
        "stat_returned": stat_returned,
    }


def get_transaction_details(conn, transaction_id):
    """Full detail view sa usa ka transaction - dungan sa was_returned_late
    flag ug ang renewal trail (audit_log entries sa renew_transaction)."""
    transaction = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               borrower_staff.username AS borrowed_by_username,
               returner_staff.username AS returned_by_username
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        LEFT JOIN staff AS borrower_staff ON transactions.borrowed_by_staff_id = borrower_staff.id
        LEFT JOIN staff AS returner_staff ON transactions.returned_by_staff_id = returner_staff.id
        WHERE transactions.id = ?
        """,
        (transaction_id,),
    ).fetchone()

    if transaction is None:
        return None, (jsonify({"error": "Transaction not found"}), 404)

    transaction = dict(transaction)
    # Late-at-return: due_date always reflects the CURRENT (possibly renewed)
    # due date, so comparing it against time_in already accounts for any
    # extension - correctly answers "was this returned after whatever its
    # due date was at the time", not just its original one.
    transaction["was_returned_late"] = bool(
        transaction["status"] == "returned"
        and transaction["time_in"]
        and transaction["due_date"]
        and transaction["time_in"] > transaction["due_date"]
    )

    # Renewal trail - services/transaction_service.py's own renew_loan logs
    # one 'renewed_loan' audit_log row per extension (see routes/transactions.py's
    # renew-form submit), each with the acting staff_id and an
    # already-human-readable "Due date extended from X to Y" details string.
    renewals = conn.execute(
        """
        SELECT audit_log.details, audit_log.created_at, staff.username AS staff_username
        FROM audit_log
        LEFT JOIN staff ON audit_log.staff_id = staff.id
        WHERE audit_log.action = 'renewed_loan'
          AND audit_log.target_table = 'transactions' AND audit_log.target_id = ?
        ORDER BY audit_log.created_at ASC
        """,
        (transaction_id,),
    ).fetchall()
    transaction["renewals"] = [dict(r) for r in renewals]

    return transaction, None


# # # BORROW (API action) -----------------------------------------------


def start_checkout_session(conn, member_barcode, member_id, staff_id):
    """Mag-start ug bag-ong checkout session (ang "cart") para sa member -
    pwede member_barcode o member_id ang gamiton. Naay re-check sa overdue
    status ug restriction dinhi mismo, dili lang sa first eligibility check,
    para dili maka-build ug cart nga dili man diay ma-checkout."""
    if member_id:
        member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    else:
        member = conn.execute(
            "SELECT * FROM members WHERE UPPER(barcode_id) = UPPER(?)", (member_barcode,)
        ).fetchone()

    if member is None:
        label = member_barcode if member_barcode else "record"
        return None, (jsonify({"error": f"Member {label} not found", "code": "not_found"}), 404)

    if not member["is_active"]:
        return None, (jsonify({"error": "This member account is not active"}), 403)

    # Re-check overdue status now, not just at commit, so staff don't build an uncheckoutable cart.
    check_and_restrict_overdue_member(member["id"], conn)
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member["id"],)).fetchone()

    if member["is_restricted"]:
        return None, (
            jsonify(
                {
                    "error": "This member is restricted",
                    "reason": member["restriction_reason"],
                }
            ),
            403,
        )

    existing_open = conn.execute(
        "SELECT id FROM checkout_sessions WHERE member_id = ? AND status = 'open'",
        (member["id"],),
    ).fetchone()

    if existing_open is not None:
        return None, (
            jsonify(
                {
                    "error": "This member already has an open checkout session",
                    "checkout_session_id": existing_open["id"],
                }
            ),
            409,
        )

    try:
        cursor = conn.execute(
            "INSERT INTO checkout_sessions (member_id, staff_id, last_activity_at) VALUES (?, ?, datetime('now', 'localtime'))",
            (member["id"], staff_id),
        )
        conn.commit()
        session_id = cursor.lastrowid
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="started_checkout_session",
        target_table="checkout_sessions",
        target_id=session_id,
        target_name=full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix']),
        details=(
            f"Started checkout session for member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}"
        ),
    )

    return {
        "message": "Checkout session started",
        "checkout_session_id": session_id,
        "member_id": member["id"],
        "member_first_name": member["first_name"],
    }, None


def list_available_assets_for_checkout(conn):
    """Para sa "Show available assets" flyout - `code` kay ang barcode_id,
    capped ang result, naa nay category. Ang consumable "available" basta
    naay stock, dili nya i-consider ang status column (kay always-'available'
    man na siya) - tan-awa asset_service.py's asset_type docs."""
    assets = conn.execute(
        """
        SELECT assets.barcode_id AS code, assets.name, categories.name AS category,
               assets.asset_type, assets.quantity
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE assets.deleted_at IS NULL
          AND (
              (assets.asset_type = 'consumable' AND assets.quantity > 0)
              OR (assets.asset_type != 'consumable' AND assets.status = 'available')
          )
        ORDER BY assets.name
        LIMIT ?
        """,
        (MAX_AVAILABLE_ASSETS_LISTED,),
    ).fetchall()
    return {"assets": [dict(row) for row in assets]}


def list_available_members_for_checkout(conn):
    """Para sa "Show available members" flyout sa checkout Step 1 - mirror ra
    ni sa list_available_assets_for_checkout sa taas. Ga-lista ug tanang
    active member, restricted man o dili - ang restricted ones (overdue o
    damaged-return holds, parehas ra ka rule sa start_checkout_session) gi-
    include gihapon pero flagged via `restricted`/`restriction_reason` para
    ma-grey-out sa UI dungan sa reason, dili lang basta i-hide nga morag
    nawala nalang ang member sa roster."""
    members = conn.execute(
        """
        SELECT members.barcode_id AS code,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.is_restricted, members.restriction_reason,
               departments.name AS department
        FROM members
        LEFT JOIN departments ON members.department_id = departments.id
        WHERE members.is_active = 1
        ORDER BY members.is_restricted, members.last_name, members.first_name
        LIMIT ?
        """,
        (MAX_AVAILABLE_MEMBERS_LISTED,),
    ).fetchall()
    return {
        "members": [
            {
                "code": row["code"],
                "name": full_name(row["first_name"], row["last_name"], row["middle_name"], row["suffix"]),
                "department": row["department"],
                "restricted": bool(row["is_restricted"]),
                "restriction_reason": row["restriction_reason"],
            }
            for row in members
        ]
    }


def add_item_to_checkout_session(conn, session_id, staff_id, asset_code, rfid_code=None, quantity=1):
    """Mag-scan ug usa ka asset paingon sa cart - gi-claim atomically (available
    -> reserved) sa una pa i-insert ang row. Optional ang rfid_code - kung
    naay second confirmatory scan (RFID tag, barcode balik, o asset ID), gi-
    cross-check ni batok sa asset's identifiers. Tan-awa _verify_second_scan().

    `quantity` naay meaning para ra sa consumable asset (services/
    asset_service.py's asset_type) - gi-ignore para sa serialized asset kay
    always usa ra ka unit man na. Tan-awa ang branch sa ubos kung nganong
    walay status CAS ang consumable."""
    session_probe = conn.execute(
        "SELECT member_id, staff_id FROM checkout_sessions WHERE id = ?", (session_id,)
    ).fetchone()

    if session_probe is None:
        return None, (jsonify({"error": "Checkout session not found"}), 404)

    error = require_session_owner(session_probe, staff_id)
    if error:
        return None, error

    _session, error = get_active_checkout_session(session_id, session_probe["member_id"], conn)
    if error:
        return None, error

    # Some readers set to type scans as keyboard input only type the decimal
    # value of an EPC's last 3 bytes, not the full hex ID - see
    # decimal_scan_hex_suffix()'s docstring. That extra LIKE branch is a
    # harmless no-op when asset_code isn't a plausible decimal scan.
    asset = conn.execute(
        """
        SELECT * FROM assets
        WHERE (
            UPPER(barcode_id) = UPPER(?)
            OR UPPER(rfid_id) = UPPER(?)
            OR UPPER(rfid_id) LIKE '%' || ?
        ) AND deleted_at IS NULL
        """,
        (asset_code, asset_code, decimal_scan_hex_suffix(asset_code)),
    ).fetchone()

    if asset is None:
        return None, (jsonify({"error": "No asset found for that code", "code": "not_found"}), 404)

    rfid_error = _verify_second_scan(asset, rfid_code)
    if rfid_error:
        return None, rfid_error

    already_in_cart = conn.execute(
        "SELECT id, quantity FROM checkout_session_items WHERE checkout_session_id = ? AND asset_id = ?",
        (session_id, asset["id"]),
    ).fetchone()

    if asset["asset_type"] == "consumable":
        return _add_consumable_to_checkout_session(conn, session_id, staff_id, asset, already_in_cart, quantity)

    if already_in_cart is not None:
        return None, (
            jsonify(
                {
                    # Never the raw scanned code (barcode or RFID tag) here -
                    # staff scanned SOMETHING to get this far, showing it back
                    # verbatim is redundant at best and a stray RFID/barcode
                    # string is meaningless to read at a glance; the asset's
                    # actual name is what staff recognize.
                    "error": f"{asset['name']} is already in the cart",
                    "code": "already_in_cart",
                    "item_id": already_in_cart["id"],
                }
            ),
            409,
        )

    if asset["status"] != "available":
        return None, (
            jsonify(
                {
                    "error": f"{asset['name']} is not available (current status: {asset['status']})",
                    "code": "not_available",
                }
            ),
            409,
        )

    try:
        claim = conn.execute(
            "UPDATE assets SET status = 'reserved' WHERE id = ? AND status = 'available'",
            (asset["id"],),
        )
        if claim.rowcount == 0:
            conn.rollback()
            return None, (
                jsonify({"error": f"{asset['name']} was just claimed by another session - try again", "code": "not_available"}),
                409,
            )

        cursor = conn.execute(
            "INSERT INTO checkout_session_items (checkout_session_id, asset_id) VALUES (?, ?)",
            (session_id, asset["id"]),
        )
        conn.execute(
            "UPDATE checkout_sessions SET last_activity_at = datetime('now', 'localtime') WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        item_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        return None, (
            jsonify({"error": f"{asset['name']} is already in the cart", "code": "already_in_cart"}),
            409,
        )
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="added_item_to_checkout_session",
        target_table="checkout_session_items",
        target_id=item_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=f"Added asset {asset['asset_id']} to checkout session {session_id}",
    )

    # Advisory only (reservations never block a checkout - see
    # reservation_service.py's module docstring), so this is purely a
    # heads-up for the scanning staff member: the soonest upcoming
    # 'scheduled' booking on this asset, if any, so they can use their own
    # judgment (give it back on time, check with the other member, etc.)
    # instead of finding out only when that booking's own time arrives.
    # Skipped entirely when the feature's off - see routes/transactions.py's
    # equivalent gate on the reservations blueprint itself.
    reservation_notice = None
    if is_feature_enabled("reservations", conn):
        upcoming = get_upcoming_reservations_for_asset(conn, asset["id"], limit=1)
        if upcoming:
            r = upcoming[0]
            reservation_notice = {
                "member_name": full_name(r["first_name"], r["last_name"], r["middle_name"], r["suffix"]),
                "start_at": r["start_at"],
                "end_at": r["end_at"],
            }

    return {
        "message": "Item added to cart",
        "item_id": item_id,
        "asset_id": asset["id"],
        "asset_name": asset["name"],
        "barcode_id": asset["barcode_id"],
        "asset_type": "serialized",
        "quantity": 1,
        "reservation_notice": reservation_notice,
    }, None


def _add_consumable_to_checkout_session(conn, session_id, staff_id, asset, existing_item, quantity):
    """Ang consumable dili gyud mag-claim sa assets.status ('available' ra
    gihapon permi) - ang quantity ra ang nag-track kung pila na'y nabilin,
    dili siya single-unit reservation. Kung ma-rescan pa ang consumable nga
    naa nay sa cart, mo-dugang ra sa cart quantity, dili mo-error ug "already
    in cart" kay mao man gyud ang obvious intent - "kuha pa ug dugang."
    Ang actual stock wala pa gi-decrement dinhi - soft check ra ni batok sa
    current on-hand quantity, dili atomic hold, mao nga pwede duha ka
    concurrent session ma-pass ni pero usa ra ang mag-succeed sa commit time
    (ang guarded UPDATE sa finish_checkout_session ang tinuod nga
    nag-prevent sa overselling)."""
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        quantity = 0
    if quantity < 1:
        return None, (jsonify({"error": "quantity must be at least 1"}), 400)

    already_requested = existing_item["quantity"] if existing_item else 0
    if quantity + already_requested > (asset["quantity"] or 0):
        return None, (
            jsonify(
                {
                    "error": f"Only {asset['quantity']} unit(s) of {asset['name']} in stock",
                    "code": "not_available",
                }
            ),
            409,
        )

    try:
        if existing_item is not None:
            conn.execute(
                "UPDATE checkout_session_items SET quantity = quantity + ? WHERE id = ?",
                (quantity, existing_item["id"]),
            )
            item_id = existing_item["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO checkout_session_items (checkout_session_id, asset_id, quantity) VALUES (?, ?, ?)",
                (session_id, asset["id"], quantity),
            )
            item_id = cursor.lastrowid
        conn.execute(
            "UPDATE checkout_sessions SET last_activity_at = datetime('now', 'localtime') WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="added_item_to_checkout_session",
        target_table="checkout_session_items",
        target_id=item_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=f"Added {quantity} unit(s) of {asset['asset_id']} to checkout session {session_id}",
    )

    return {
        "message": "Item added to cart",
        "item_id": item_id,
        "asset_id": asset["id"],
        "asset_name": asset["name"],
        "barcode_id": asset["barcode_id"],
        "asset_type": "consumable",
        "quantity": quantity + already_requested,
    }, None


def remove_item_from_checkout_session(conn, session_id, item_id, staff_id, decrement_by=None):
    """Ang decrement_by kay consumable-only siya (ang cart quantity stepper's
    "-" button) - mag-reduce ra sa line's quantity imbes tanggalon dayon,
    gawas kung mo-zero or below na - mao pa na siya tanggalon parehas sa
    serialized item. None (default) permi mag-remove sa whole line, parehas
    sa daan before pa ni na feature."""
    item = conn.execute(
        "SELECT * FROM checkout_session_items WHERE id = ? AND checkout_session_id = ?",
        (item_id, session_id),
    ).fetchone()

    if item is None:
        return None, (jsonify({"error": "Cart item not found"}), 404)

    session = conn.execute("SELECT * FROM checkout_sessions WHERE id = ?", (session_id,)).fetchone()

    error = require_session_owner(session, staff_id)
    if error:
        return None, error

    if session["status"] != "open":
        return None, (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    asset = conn.execute(
        "SELECT asset_id, name FROM assets WHERE id = ?", (item["asset_id"],)
    ).fetchone()

    partial = decrement_by is not None and 0 < decrement_by < item["quantity"]

    try:
        if partial:
            conn.execute(
                "UPDATE checkout_session_items SET quantity = quantity - ? WHERE id = ?",
                (decrement_by, item_id),
            )
        else:
            conn.execute("DELETE FROM checkout_session_items WHERE id = ?", (item_id,))
        conn.execute(
            "UPDATE assets SET status = 'available' WHERE id = ? AND status = 'reserved'",
            (item["asset_id"],),
        )
        conn.execute(
            "UPDATE checkout_sessions SET last_activity_at = datetime('now', 'localtime') WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="removed_item_from_checkout_session",
        target_table="checkout_session_items",
        target_id=item_id,
        target_name=f"{asset['asset_id']} - {asset['name']}" if asset else None,
        details=(
            f"Reduced quantity by {decrement_by} for asset in checkout session {session_id}"
            if partial
            else f"Removed asset from checkout session {session_id}"
        ),
    )

    if partial:
        new_quantity = conn.execute(
            "SELECT quantity FROM checkout_session_items WHERE id = ?", (item_id,)
        ).fetchone()["quantity"]
        return {"message": "Quantity updated", "removed": False, "quantity": new_quantity}, None

    return {"message": "Item removed from cart"}, None


def get_reserved_asset_hold(conn, asset_id):
    """Para sa admin-only "release reservation" confirm modal (Assets page).
    Mangita kung kinsa gyud ang naka-hold sa 'reserved' asset - unsa nga
    member's cart na siya, ug unsa nga staff ang nag-start ana nga checkout -
    para makita sa admin unsa gyud iyang i-bypass before mo-confirm, dili
    lang ang status sa asset."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, ({"error": "Asset not found"}, 404, {})

    if asset["status"] != "reserved":
        return None, ({"error": "This asset is not currently reserved"}, 409, {"asset": asset})

    hold = conn.execute(
        """
        SELECT checkout_session_items.id AS item_id, checkout_sessions.id AS session_id,
               checkout_sessions.staff_id, staff.username AS staff_username,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM checkout_session_items
        JOIN checkout_sessions ON checkout_sessions.id = checkout_session_items.checkout_session_id
        JOIN members ON members.id = checkout_sessions.member_id
        LEFT JOIN staff ON staff.id = checkout_sessions.staff_id
        WHERE checkout_session_items.asset_id = ? AND checkout_sessions.status = 'open'
        """,
        (asset_id,),
    ).fetchone()

    return {"asset": asset, "hold": hold}, None


def release_reserved_asset(conn, asset_id, staff_id):
    """Admin-only bypass para sa asset nga stuck sa 'reserved' kay ang nag-
    checkout niya nag-logout (o na-dead ang tab/device) nga wa i-cancel o
    i-complete ang checkout - normally mo-clear ra na iyaha after sa admin-
    configurable checkout_session_stale_minutes window (System > Checkout
    Session Timeout; services/checkout_service.py's scheduled sweep), pero
    dili na kinahanglan hulaton sa admin ang timer para mabalik ang tool sa
    circulation. Lahi sa remove_item_from_checkout_session, deliberately
    gi-skip diri ang require_session_owner - ang tibuok point man gud ani
    kay i-bypass ang abandoned cart sa lain."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, ({"error": "Asset not found"}, 404, {})

    if asset["status"] != "reserved":
        return None, ({"error": "This asset is not currently reserved"}, 409, {"asset": asset})

    item = conn.execute(
        """
        SELECT checkout_session_items.id AS item_id, checkout_sessions.id AS session_id,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM checkout_session_items
        JOIN checkout_sessions ON checkout_sessions.id = checkout_session_items.checkout_session_id
        JOIN members ON members.id = checkout_sessions.member_id
        WHERE checkout_session_items.asset_id = ? AND checkout_sessions.status = 'open'
        """,
        (asset_id,),
    ).fetchone()

    if item is None:
        # Reserved with no open cart item behind it shouldn't happen, but
        # don't leave the asset stuck if it does - just clear the status.
        conn.execute("UPDATE assets SET status = 'available' WHERE id = ? AND status = 'reserved'", (asset_id,))
        conn.commit()
        return {"message": "Reservation cleared"}, None

    holder_name = full_name(item["first_name"], item["last_name"], item["middle_name"], item["suffix"])

    try:
        conn.execute("DELETE FROM checkout_session_items WHERE id = ?", (item["item_id"],))
        conn.execute(
            "UPDATE assets SET status = 'available' WHERE id = ? AND status = 'reserved'",
            (asset_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="admin_released_reserved_asset",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=(
            f"Admin bypassed reservation on {asset['asset_id']} - was held in "
            f"{holder_name}'s in-progress checkout (session {item['session_id']})"
        ),
    )

    return {"message": "Reservation released - asset is available again", "asset_id_code": asset["asset_id"]}, None


def get_checkout_session_detail(conn, session_id, staff_id):
    """Full detail sa usa ka checkout session - member info dungan sa cart
    items - naay owner check para dili malantaw sa laing staff."""
    session = conn.execute(
        """
        SELECT checkout_sessions.*, members.first_name, members.last_name,
               members.middle_name, members.suffix, members.barcode_id AS member_barcode
        FROM checkout_sessions
        JOIN members ON checkout_sessions.member_id = members.id
        WHERE checkout_sessions.id = ?
        """,
        (session_id,),
    ).fetchone()

    if session is None:
        return None, (jsonify({"error": "Checkout session not found"}), 404)

    error = require_session_owner(session, staff_id)
    if error:
        return None, error

    items = conn.execute(
        """
        SELECT checkout_session_items.id AS item_id, assets.id AS asset_id,
               assets.asset_id AS asset_code, assets.name AS asset_name,
               checkout_session_items.added_at
        FROM checkout_session_items
        JOIN assets ON checkout_session_items.asset_id = assets.id
        WHERE checkout_session_items.checkout_session_id = ?
        ORDER BY checkout_session_items.added_at
        """,
        (session_id,),
    ).fetchall()

    return {**dict(session), "items": [dict(row) for row in items]}, None


# # # RETURN (API action) -----------------------------------------------


def _finalize_return(transaction, asset, member, staff_id, condition_in, return_remarks, conn):
    """Core return logic nga gi-share sa scan-return ug row-action return -
    ang caller ang tag-iya sa iyang own commit/log."""
    final_condition = condition_in if condition_in else asset["condition"]
    is_disposed = final_condition == "disposed"
    new_status = "available" if final_condition == "functional" else "maintenance"

    conn.execute(
        """
        UPDATE transactions
        SET time_in = datetime('now', 'localtime'), status = 'returned',
            returned_by_staff_id = ?, return_remarks = ?, condition_in = ?
        WHERE id = ?
        """,
        (staff_id, return_remarks, final_condition, transaction["id"]),
    )

    if is_disposed:
        # disposed is terminal - soft-delete now instead of parking it in 'maintenance'
        conn.execute(
            "UPDATE assets SET status = ?, condition = ?, deleted_at = datetime('now', 'localtime') WHERE id = ?",
            (new_status, final_condition, asset["id"]),
        )
    else:
        conn.execute(
            "UPDATE assets SET status = ?, condition = ? WHERE id = ?",
            (new_status, final_condition, asset["id"]),
        )

    conn.commit()
    check_and_lift_restriction_if_resolved(member["id"], conn)
    if final_condition != "functional":
        check_and_restrict_damaged_returns(member["id"], conn)

    return {
        "final_condition": final_condition,
        "is_disposed": is_disposed,
        "new_status": new_status,
    }


def _find_open_transaction_for_scan(asset_code, member_barcode, conn, rfid_code=None):
    """Mag-validate sa asset/member barcode pair para sa scan return - pure
    lookup ra ni, walay mutation. Optional ang rfid_code - kung naay second
    confirmatory scan sa gi-return na tool (RFID tag, barcode balik, o asset
    ID), gi-cross-check batok sa asset's identifiers, para dili maka-pass
    ang barcode nga gi-peel gikan sa original tool ug gibutang sa substitute
    tool bisan pa nag-match ang barcode. Tan-awa _verify_second_scan()."""
    # Some readers set to type scans as keyboard input only type the decimal
    # value of an EPC's last 3 bytes, not the full hex ID - see
    # decimal_scan_hex_suffix()'s docstring. That extra LIKE branch is a
    # harmless no-op when asset_code isn't a plausible decimal scan.
    asset = conn.execute(
        """
        SELECT * FROM assets
        WHERE (
            UPPER(barcode_id) = UPPER(?)
            OR UPPER(rfid_id) = UPPER(?)
            OR UPPER(rfid_id) LIKE '%' || ?
        ) AND deleted_at IS NULL
        """,
        (asset_code, asset_code, decimal_scan_hex_suffix(asset_code)),
    ).fetchone()
    if asset is None:
        return None, None, None, (jsonify({"error": "No asset found for that code", "code": "not_found"}), 404)

    rfid_error = _verify_second_scan(asset, rfid_code)
    if rfid_error:
        return None, None, None, rfid_error

    transaction = conn.execute(
        "SELECT * FROM transactions WHERE asset_id = ? AND status = 'borrowed'",
        (asset["id"],),
    ).fetchone()
    if transaction is None:
        return None, None, None, (
            # Never the raw scanned code (barcode or RFID tag) - see the
            # matching comment on the checkout-side "already in the cart"
            # error for why.
            jsonify({"error": f"{asset['name']} has no open borrow transaction", "code": "not_available"}),
            400,
        )

    member = conn.execute(
        "SELECT * FROM members WHERE UPPER(barcode_id) = UPPER(?)", (member_barcode,)
    ).fetchone()
    if member is None:
        return None, None, None, (jsonify({"error": f"Member {member_barcode} not found", "code": "not_found"}), 404)

    if member["id"] != transaction["member_id"]:
        return (
            None,
            None,
            None,
            (jsonify({"error": "This asset was borrowed by someone else. member mismatch."}), 403),
        )

    return transaction, asset, member, None


def _log_return(conn, staff_id, action_target_id, asset, member, result, batch=False):
    """Shared audit_log entry para sa gi-return na asset - naay dungan pang
    'disposed_asset' log kung disposed ang final condition. Gina-pasa ang
    conn ngadto sa log_action para dili mag-open og bag-ong connection
    matag return (importante gyud sa batch_return's per-row loop)."""
    log_action(
        conn=conn,
        staff_id=staff_id,
        action="returned_asset",
        target_table="transactions",
        target_id=action_target_id,
        target_name=f"{asset['asset_id']} - {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
        details=(
            f"Asset {asset['asset_id']} returned{' (batch)' if batch else ''} by member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])} "
            f"- condition: {result['final_condition']}"
        ),
    )
    if result["is_disposed"]:
        log_action(
            conn=conn,
            staff_id=staff_id,
            action="disposed_asset",
            target_table="assets",
            target_id=asset["id"],
            target_name=f"{asset['asset_id']} - {asset['name']}",
            details=f"Asset {asset['asset_id']} disposed on return and removed from circulation",
        )


def return_asset_by_scan(conn, asset_code, member_barcode, staff_id, condition_in, return_remarks, rfid_code=None):
    """Ang actual return-by-scan flow - mag-lookup sa transaction/asset/member
    via _find_open_transaction_for_scan, then i-finalize ang return."""
    transaction, asset, member, error = _find_open_transaction_for_scan(
        asset_code, member_barcode, conn, rfid_code
    )
    if error:
        return None, error

    try:
        result = _finalize_return(transaction, asset, member, staff_id, condition_in, return_remarks, conn)
    except Exception:
        conn.rollback()
        raise

    _log_return(conn, staff_id, asset["id"], asset, member, result)

    return {
        "message": "asset returned successfully",
        "condition": result["final_condition"],
        "status": result["new_status"],
    }, None


def list_members_with_open_loans(conn):
    """Para sa "Show members with loans" flyout sa Return Items modal - parehas
    ra ka idea sa list_available_members_for_checkout, pero baliktad ang
    direction: ang members ra nga naay gi-borrow currently ang worth i-browse
    diri, kay walay pulos mo-pick ug member nga wala man diay ibalik."""
    members = conn.execute(
        """
        SELECT members.barcode_id AS code,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               COUNT(transactions.id) AS loan_count
        FROM members
        JOIN transactions ON transactions.member_id = members.id AND transactions.status = 'borrowed'
        GROUP BY members.id
        ORDER BY members.last_name, members.first_name
        LIMIT ?
        """,
        (MAX_AVAILABLE_MEMBERS_LISTED,),
    ).fetchall()
    return {
        "members": [
            {
                "code": row["code"],
                "name": full_name(row["first_name"], row["last_name"], row["middle_name"], row["suffix"]),
                "loan_count": row["loan_count"],
            }
            for row in members
        ]
    }


def get_member_open_loans(conn, member_barcode):
    """Ga-lista kung unsay gi-borrow sa member - zero loans normal ra na 200
    dungan sa empty list, dili ni error."""
    member = conn.execute(
        "SELECT * FROM members WHERE UPPER(barcode_id) = UPPER(?)", (member_barcode,)
    ).fetchone()
    if member is None:
        return None, (jsonify({"error": "member not found"}), 404)

    loans = conn.execute(
        """
        SELECT transactions.id AS transaction_id, assets.name AS asset_name,
               assets.barcode_id AS asset_barcode, transactions.due_date
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        WHERE transactions.member_id = ? AND transactions.status = 'borrowed'
        ORDER BY assets.name
        """,
        (member["id"],),
    ).fetchall()

    return {
        "member_name": full_name(
            member["first_name"], member["last_name"], member["middle_name"], member["suffix"]
        ),
        "loans": [dict(row) for row in loans],
    }, None


def lookup_return_scan(conn, asset_code, member_barcode, rfid_code=None):
    """Read-only per-scan lookup para sa scan-return cart - wala pa mag-mutate
    hangtod mo-submit ang batch-return."""
    transaction, asset, _member, error = _find_open_transaction_for_scan(
        asset_code, member_barcode, conn, rfid_code
    )
    if error:
        return None, error

    return {
        "transaction_id": transaction["id"],
        "asset_name": asset["name"],
        "asset_barcode": asset["barcode_id"],
        "rfid_verified": bool(rfid_code),
    }, None


def get_return_confirm_transaction(conn, transaction_id):
    """Mag-open sa row-action return modal. Read-only lookup ra ni - ang
    actual mutation naa sa return_transaction_by_id() sa ubos, pag-submit."""
    transaction = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               assets.condition AS asset_condition, members.first_name, members.last_name,
               members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.id = ?
        """,
        (transaction_id,),
    ).fetchone()

    if transaction is None:
        return None, (jsonify({"error": "Transaction not found"}), 404)
    if transaction["status"] != "borrowed":
        return None, (jsonify({"error": "This transaction has already been returned"}), 400)

    return transaction, None


def return_transaction_by_id(conn, transaction_id, staff_id, condition_in, return_remarks):
    """Row-action return - gi-skip ang barcode lookup, mag-confirm ra nga
    open pa gyud ang transaction before mag-finalize."""
    transaction = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if transaction is None:
        return None, (jsonify({"error": "Transaction not found"}), 404)
    if transaction["status"] != "borrowed":
        return None, (jsonify({"error": "This transaction has already been returned"}), 400)

    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (transaction["asset_id"],)).fetchone()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (transaction["member_id"],)).fetchone()

    try:
        result = _finalize_return(transaction, asset, member, staff_id, condition_in, return_remarks, conn)
    except Exception:
        conn.rollback()
        raise

    _log_return(conn, staff_id, transaction["id"], asset, member, result)

    return {
        "message": f"{asset['asset_id']} returned successfully",
        "condition": result["final_condition"],
        "status": result["new_status"],
    }, None


# # # BATCH RETURN (API action) -------------------------------------------


def get_batch_return_candidates(conn, ids):
    """Silent ra i-drop ang ids nga wala na o na-return na - dili mo-error
    kada usa."""
    placeholders = ",".join("?" for _ in ids)
    transactions = conn.execute(
        f"""
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.id IN ({placeholders}) AND transactions.status = 'borrowed'
        ORDER BY assets.name
        """,
        ids,
    ).fetchall()

    if not transactions:
        return None, (jsonify({"error": "None of the selected loans are still open"}), 400)
    return transactions, None


def batch_return(conn, items, return_remarks, staff_id):
    """Mag-apply ug condition/remarks sa selected transactions - best-effort
    ni, usa ka bad row dili mo-abort sa uban. `items` kay list of
    (transaction_id, condition_in) nga na-validate na sa route."""
    returned_ids = []
    skipped_ids = []

    for txn_id, condition_in in items:
        transaction = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if transaction is None or transaction["status"] != "borrowed":
            skipped_ids.append(txn_id)
            continue

        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (transaction["asset_id"],)).fetchone()
        member = conn.execute("SELECT * FROM members WHERE id = ?", (transaction["member_id"],)).fetchone()

        try:
            result = _finalize_return(transaction, asset, member, staff_id, condition_in, return_remarks, conn)
        except Exception:
            conn.rollback()
            skipped_ids.append(txn_id)
            continue

        returned_ids.append(txn_id)
        _log_return(conn, staff_id, transaction["id"], asset, member, result, batch=True)

    return {
        "message": f"{len(returned_ids)} asset(s) returned"
        + (f", {len(skipped_ids)} skipped" if skipped_ids else ""),
        "returned": returned_ids,
        "skipped": skipped_ids,
    }, None


# # # RENEW / EXTEND DUE DATE (API action) ---------------------------------


def get_renewable_transaction(conn, transaction_id):
    """Mag-open sa extend-due-date modal - read-only lookup, parehas ka split
    sa get_return_confirm_transaction()/return_transaction_by_id()."""
    transaction = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.id = ?
        """,
        (transaction_id,),
    ).fetchone()

    if transaction is None:
        return None, (jsonify({"error": "Transaction not found"}), 404)
    if transaction["status"] != "borrowed":
        return None, (jsonify({"error": "Only an active loan can be extended"}), 400)

    return transaction, None


def renew_transaction(conn, transaction_id, new_due_date, staff_id):
    """Mag-push sa due date paatras (o mag-correct ana) - dili gyud ma-touch
    ang status. Pwede maka-lift sa overdue restriction sa member kung
    ma-un-overdue siya after sa extension."""
    transaction = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if transaction is None:
        return None, (jsonify({"error": "Transaction not found"}), 404)
    if transaction["status"] != "borrowed":
        return None, (jsonify({"error": "Only an active loan can be extended"}), 400)

    old_due_date = transaction["due_date"]
    conn.execute("UPDATE transactions SET due_date = ? WHERE id = ?", (new_due_date, transaction_id))
    conn.commit()

    # Pushing the due date forward can un-overdue a member; same check the return flow runs.
    check_and_lift_restriction_if_resolved(transaction["member_id"], conn)

    # look up asset/member names for a readable Activity Log entry
    txn_asset = conn.execute(
        "SELECT asset_id FROM assets WHERE id = ?", (transaction["asset_id"],)
    ).fetchone()
    txn_member = conn.execute(
        "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?",
        (transaction["member_id"],),
    ).fetchone()

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="renewed_loan",
        target_table="transactions",
        target_id=transaction_id,
        target_name=(
            f"{txn_asset['asset_id']} - {full_name(txn_member['first_name'], txn_member['last_name'], txn_member['middle_name'], txn_member['suffix'])}"
            if txn_asset and txn_member
            else None
        ),
        details=f"Due date extended from {old_due_date or '-'} to {new_due_date}",
    )

    return {"message": "Due date updated", "due_date": new_due_date}, None


# # # CHECKOUT SESSIONS (API action) -------------------------------------


def list_open_checkout_sessions(conn):
    """Tanang open checkout sessions across staff members (admin overview),
    dungan sa item_count per session."""
    sessions = conn.execute(
        """
        SELECT checkout_sessions.*, members.first_name, members.last_name,
               members.middle_name, members.suffix, members.barcode_id AS member_barcode,
               (SELECT COUNT(*) FROM checkout_session_items
                WHERE checkout_session_id = checkout_sessions.id) AS item_count
        FROM checkout_sessions
        JOIN members ON checkout_sessions.member_id = members.id
        WHERE checkout_sessions.status = 'open'
        ORDER BY checkout_sessions.created_at ASC
        """
    ).fetchall()
    return [dict(row) for row in sessions]


def finish_checkout_session(conn, session_id, staff_id, due_date, default_loan_days):
    """Mao ni ang actual commit point - walay malanding sa `transactions`
    hangtod ni siya mo-run; ang tanang gi-scan before pa ani reservation
    ra gihapon. Mag-re-check pud ug eligibility (overdue, restriction,
    concurrent-loan cap) kay pwede naman-usab ang status samtang nag-scan
    pa - consumables gi-consume dayon (quantity decrement), serialized ang
    ma-'borrowed' na proper transactions row."""
    if due_date is None:
        due_date = (datetime.now() + timedelta(days=default_loan_days)).strftime("%Y-%m-%d %H:%M:%S")

    session = conn.execute("SELECT * FROM checkout_sessions WHERE id = ?", (session_id,)).fetchone()

    if session is None:
        return None, (jsonify({"error": "Checkout session not found"}), 404)

    error = require_session_owner(session, staff_id)
    if error:
        return None, error

    if session["status"] != "open":
        return None, (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    member = conn.execute("SELECT * FROM members WHERE id = ?", (session["member_id"],)).fetchone()

    # re-check eligibility at commit time, not just cart-creation - status may have changed while scanning
    if check_and_restrict_overdue_member(member["id"], conn):
        return None, (
            jsonify(
                {
                    "error": "This member is restricted",
                    "reason": "Has an overdue asset that has not been returned",
                }
            ),
            403,
        )

    member = conn.execute("SELECT * FROM members WHERE id = ?", (session["member_id"],)).fetchone()
    if member["is_restricted"]:
        return None, (
            jsonify(
                {
                    "error": "This member is restricted",
                    "reason": member["restriction_reason"],
                }
            ),
            403,
        )

    current_loan_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE member_id = ? AND status = 'borrowed'",
        (member["id"],),
    ).fetchone()["cnt"]

    items = conn.execute(
        "SELECT * FROM checkout_session_items WHERE checkout_session_id = ?", (session_id,)
    ).fetchall()

    if not items:
        return None, (
            jsonify({"error": "Cart is empty - scan at least one asset before checking out"}),
            400,
        )

    # One batched lookup instead of a per-item SELECT - reused below both for
    # the loan-cap check (consumables don't count, see comment) and again in
    # the commit loop, so asset rows aren't fetched twice per item.
    asset_rows_by_id = {
        row["id"]: row
        for row in conn.execute(
            f"SELECT * FROM assets WHERE id IN ({','.join('?' * len(items))})",
            [item["asset_id"] for item in items],
        ).fetchall()
    }

    # Consumables are consumed on the spot (see the loop below), never
    # 'borrowed' - they shouldn't count against the member's concurrent-loan
    # cap, only the serialized items actually going out on loan do.
    borrowable_item_count = sum(
        1 for item in items if asset_rows_by_id[item["asset_id"]]["asset_type"] != "consumable"
    )

    max_loans = get_max_concurrent_loans(member["member_type"], conn)
    if current_loan_count + borrowable_item_count > max_loans:
        return None, (
            jsonify(
                {
                    "error": (
                        f"This checkout would put the member at {current_loan_count + borrowable_item_count} "
                        f"concurrent loans, exceeding the maximum of {max_loans}"
                    ),
                    "current_loans": current_loan_count,
                    "cart_size": borrowable_item_count,
                }
            ),
            403,
        )

    # Guard-clause races (item claimed/consumed by someone else between the
    # scan and this commit) are reported back as a normal (data, error)
    # 409 tuple - not raised - so the route's already-established JSON error
    # pattern handles it instead of falling through to a generic 500.
    race_conflict_asset_id = None
    try:
        for item in items:
            asset = asset_rows_by_id[item["asset_id"]]

            if asset["asset_type"] == "consumable":
                # No borrowed/reserved lifecycle for a consumable - decrement
                # stock directly (guarded the same way the serialized CAS
                # below guards status) and log it as an already-'returned'
                # transaction (nothing to return - it's consumed on
                # checkout), which keeps it out of the borrowed/overdue
                # sweeps for free without needing a new transactions.status
                # value. See services/asset_service.py's asset_type docs.
                claim = conn.execute(
                    "UPDATE assets SET quantity = quantity - ? WHERE id = ? AND asset_type = 'consumable' AND quantity >= ?",
                    (item["quantity"], item["asset_id"], item["quantity"]),
                )
                if claim.rowcount == 0:
                    race_conflict_asset_id = item["asset_id"]
                    break

                conn.execute(
                    """
                    INSERT INTO transactions
                        (checkout_session_id, asset_id, member_id, borrowed_by_staff_id, returned_by_staff_id,
                         due_date, condition_out, condition_in, time_in, status)
                    VALUES (?, ?, ?, ?, ?, NULL, ?, ?, datetime('now', 'localtime'), 'returned')
                    """,
                    (
                        session_id,
                        item["asset_id"],
                        member["id"],
                        staff_id,
                        staff_id,
                        asset["condition"],
                        asset["condition"],
                    ),
                )
                continue

            claim = conn.execute(
                "UPDATE assets SET status = 'borrowed' WHERE id = ? AND status = 'reserved'",
                (item["asset_id"],),
            )
            if claim.rowcount == 0:
                race_conflict_asset_id = item["asset_id"]
                break

            # Feeds the usage-based maintenance trigger (services/
            # asset_service.py's get_maintenance_due_assets) - resets to 0
            # whenever resolve_maintenance runs, so this only counts
            # checkouts since the asset was last serviced.
            conn.execute(
                "UPDATE assets SET checkouts_since_maintenance = checkouts_since_maintenance + 1 WHERE id = ?",
                (item["asset_id"],),
            )

            conn.execute(
                """
                INSERT INTO transactions
                    (checkout_session_id, asset_id, member_id, borrowed_by_staff_id, due_date, condition_out, status)
                VALUES (?, ?, ?, ?, ?, ?, 'borrowed')
                """,
                (
                    session_id,
                    item["asset_id"],
                    member["id"],
                    staff_id,
                    due_date,
                    asset["condition"],
                ),
            )

        if race_conflict_asset_id is not None:
            conn.rollback()
        else:
            conn.execute(
                "DELETE FROM checkout_session_items WHERE checkout_session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE checkout_sessions SET status = 'completed', completed_at = datetime('now', 'localtime') WHERE id = ?",
                (session_id,),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise

    if race_conflict_asset_id is not None:
        # Cart itself is untouched (rolled back above) - safe to retry after
        # a rescan; this is the same 409 shape add_item_to_checkout_session
        # already uses for its own claim races.
        return None, (
            jsonify(
                {
                    "error": "One or more items changed status while checking out - rescan the cart and try again",
                    "asset_id": race_conflict_asset_id,
                }
            ),
            409,
        )

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="checked_out_session",
        target_table="checkout_sessions",
        target_id=session_id,
        target_name=full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix']),
        details=(
            f"Checked out {len(items)} asset(s) to member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}"
        ),
    )

    return {"message": "Checkout complete", "items_borrowed": len(items)}, None


def cancel_checkout_session(conn, session_id, staff_id):
    """Mag-cancel sa cart - i-release balik ang tanang reserved assets ngadto
    sa 'available' ug i-delete ang cart items, wa'y transaction nga ma-create."""
    session = conn.execute("SELECT * FROM checkout_sessions WHERE id = ?", (session_id,)).fetchone()

    if session is None:
        return None, (jsonify({"error": "Checkout session not found"}), 404)

    error = require_session_owner(session, staff_id)
    if error:
        return None, error

    if session["status"] != "open":
        return None, (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    try:
        conn.execute(
            """
            UPDATE assets SET status = 'available'
            WHERE id IN (SELECT asset_id FROM checkout_session_items WHERE checkout_session_id = ?)
            AND status = 'reserved'
            """,
            (session_id,),
        )
        conn.execute(
            "DELETE FROM checkout_session_items WHERE checkout_session_id = ?", (session_id,)
        )
        conn.execute(
            "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cancel_member = conn.execute(
            "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?",
            (session["member_id"],),
        ).fetchone()

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="cancelled_checkout_session",
        target_table="checkout_sessions",
        target_id=session_id,
        target_name=(
            full_name(cancel_member['first_name'], cancel_member['last_name'], cancel_member['middle_name'], cancel_member['suffix'])
            if cancel_member else None
        ),
        details=f"Cancelled checkout session {session_id}, released reserved assets",
    )

    return {"message": "Checkout session cancelled"}, None
