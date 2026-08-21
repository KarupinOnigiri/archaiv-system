"""Query-building, checkout-session/loan business logic, and export shaping for
routes/transactions.py. Every function takes an already-open `conn` (the route
owns open/close) and returns `(data, error)` — `error` is a ready `(jsonify(...), status)`
tuple on failure, `data` is a plain dict/list/row on success — so routes stay a thin
parse-args -> call service -> jsonify layer, matching utils/validators.py's convention.
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
from utils.constants import SESSION_STALE_MINUTES
from utils.formatters import full_name
from utils.logger import log_action
from utils.validators import validate_date_string

# Fallback only -- real default lives in app_settings (settings_service.get_default_loan_days)
DEFAULT_LOAN_DAYS = 2
# Fallback only -- real caps live in the loan_limits table (see get_max_concurrent_loans below)
MAX_CONCURRENT_LOANS = 10
MAX_AVAILABLE_ASSETS_LISTED = 300
MAX_BATCH_RETURN_SIZE = 50
VALID_RETURN_CONDITIONS = ("functional", "inoperable", "for_repair", "disposed")


def get_max_concurrent_loans(member_type, conn):
    """Looks up the concurrent-loan cap for a member_type, falling back to
    MAX_CONCURRENT_LOANS if loan_limits has no row for it."""
    row = conn.execute(
        "SELECT max_concurrent_loans FROM loan_limits WHERE member_type = ?",
        (member_type,),
    ).fetchone()
    return row["max_concurrent_loans"] if row else MAX_CONCURRENT_LOANS


def get_active_checkout_session(session_id, member_id, conn):
    """Returns (session_row, error) for an open checkout session; lazily closes stale ones."""
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
    if datetime.now() - last_activity_at > timedelta(minutes=SESSION_STALE_MINUTES):
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
    """Errors if this session belongs to another staff member; no owner on record counts as accessible."""
    if session["staff_id"] is not None and session["staff_id"] != staff_id:
        return jsonify({"error": "This checkout session belongs to another staff member"}), 403
    return None


def load_open_sessions_for_staff(staff_id, conn):
    """All of a staff member's open checkout sessions with items; single source for the dock/banner."""
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

    results = []
    for session in sessions:
        last_activity_at = datetime.strptime(
            session["last_activity_at"] or session["created_at"], "%Y-%m-%d %H:%M:%S"
        )
        if datetime.now() - last_activity_at > timedelta(minutes=SESSION_STALE_MINUTES):
            conn.execute(
                "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ? AND status = 'open'",
                (session["id"],),
            )
            conn.commit()
            continue

        items = conn.execute(
            """
            SELECT checkout_session_items.id AS id, assets.name AS asset_name
            FROM checkout_session_items
            JOIN assets ON checkout_session_items.asset_id = assets.id
            WHERE checkout_session_items.checkout_session_id = ?
            ORDER BY checkout_session_items.added_at
            """,
            (session["id"],),
        ).fetchall()

        results.append({**dict(session), "cart_items": [dict(row) for row in items]})

    return results


def load_open_session_for_staff(staff_id, conn):
    """Most recent open session, or None; thin wrapper for callers that only want one."""
    sessions = load_open_sessions_for_staff(staff_id, conn)
    return sessions[0] if sessions else None


# # # LIST / VIEW ------------------------------------------------------------


def list_transactions(conn, limit, offset):
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
    """Builds the WHERE clause + params shared by the table listing and CSV export."""
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
            "(assets.name LIKE ? OR assets.asset_id LIKE ? OR members.first_name LIKE ? OR members.last_name LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

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
    """Shared table context for view_transactions_table() and transactions_page(), so they stay in sync."""
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
    """CSV bytes of the current filtered table, ignoring sort/pagination since export means the whole set."""
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
    """The 4 stat-card numbers, same pattern as /assets/stat-cards."""
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
    return transaction, None


# # # BORROW (API action) -----------------------------------------------


def start_checkout_session(conn, member_barcode, member_id, staff_id):
    """Starts a new checkout session (the "cart") for a member; accepts either member_barcode or member_id."""
    if member_id:
        member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    else:
        member = conn.execute(
            "SELECT * FROM members WHERE UPPER(barcode_id) = UPPER(?)", (member_barcode,)
        ).fetchone()

    if member is None:
        return None, (jsonify({"error": "Member not found"}), 404)

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
    """Backs the "Show available assets" flyout; `code` is barcode_id, capped, category included."""
    assets = conn.execute(
        """
        SELECT assets.barcode_id AS code, assets.name, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE assets.status = 'available' AND assets.deleted_at IS NULL
        ORDER BY assets.name
        LIMIT ?
        """,
        (MAX_AVAILABLE_ASSETS_LISTED,),
    ).fetchall()
    return {"assets": [dict(row) for row in assets]}


def add_item_to_checkout_session(conn, session_id, staff_id, asset_code):
    """Scans one asset into the cart; claims it atomically (available -> reserved) before inserting the row."""
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

    asset = conn.execute(
        "SELECT * FROM assets WHERE (UPPER(barcode_id) = UPPER(?) OR UPPER(rfid_id) = UPPER(?)) AND deleted_at IS NULL",
        (asset_code, asset_code),
    ).fetchone()

    if asset is None:
        return None, (jsonify({"error": "Asset not found"}), 404)

    already_in_cart = conn.execute(
        "SELECT id FROM checkout_session_items WHERE checkout_session_id = ? AND asset_id = ?",
        (session_id, asset["id"]),
    ).fetchone()

    if already_in_cart is not None:
        return None, (jsonify({"error": "This asset is already in the cart"}), 409)

    if asset["status"] != "available":
        return None, (
            jsonify({"error": f"Asset is not available (current status: {asset['status']})"}),
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
                jsonify({"error": "Asset was just claimed by another session — try again"}),
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
        return None, (jsonify({"error": "This asset is already in the cart"}), 409)
    except Exception:
        conn.rollback()
        raise

    log_action(
        staff_id=staff_id,
        action="added_item_to_checkout_session",
        target_table="checkout_session_items",
        target_id=item_id,
        target_name=f"{asset['asset_id']} — {asset['name']}",
        details=f"Added asset {asset['asset_id']} to checkout session {session_id}",
    )

    return {
        "message": "Item added to cart",
        "item_id": item_id,
        "asset_id": asset["id"],
        "asset_name": asset["name"],
    }, None


def remove_item_from_checkout_session(conn, session_id, item_id, staff_id):
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

    try:
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
        staff_id=staff_id,
        action="removed_item_from_checkout_session",
        target_table="checkout_session_items",
        target_id=item_id,
        target_name=f"{asset['asset_id']} — {asset['name']}" if asset else None,
        details=f"Removed asset from checkout session {session_id}",
    )

    return {"message": "Item removed from cart"}, None


def get_checkout_session_detail(conn, session_id, staff_id):
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
    """Core return logic shared by scan-return and row-action return; callers own their own commit/log."""
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
        # disposed is terminal -- soft-delete now instead of parking it in 'maintenance'
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


def _find_open_transaction_for_scan(asset_code, member_barcode, conn):
    """Validates an asset/member barcode pair for a scan return; pure lookup, no mutation."""
    asset = conn.execute(
        "SELECT * FROM assets WHERE (UPPER(barcode_id) = UPPER(?) OR UPPER(rfid_id) = UPPER(?)) AND deleted_at IS NULL",
        (asset_code, asset_code),
    ).fetchone()
    if asset is None:
        return None, None, None, (jsonify({"error": "Asset not found"}), 404)

    transaction = conn.execute(
        "SELECT * FROM transactions WHERE asset_id = ? AND status = 'borrowed'",
        (asset["id"],),
    ).fetchone()
    if transaction is None:
        return None, None, None, (jsonify({"error": "This asset has no open borrow transaction"}), 400)

    member = conn.execute(
        "SELECT * FROM members WHERE UPPER(barcode_id) = UPPER(?)", (member_barcode,)
    ).fetchone()
    if member is None:
        return None, None, None, (jsonify({"error": "member not found"}), 404)

    if member["id"] != transaction["member_id"]:
        return (
            None,
            None,
            None,
            (jsonify({"error": "This asset was borrowed by someone else. member mismatch."}), 403),
        )

    return transaction, asset, member, None


def _log_return(staff_id, action_target_id, asset, member, result, batch=False):
    log_action(
        staff_id=staff_id,
        action="returned_asset",
        target_table="transactions",
        target_id=action_target_id,
        target_name=f"{asset['asset_id']} — {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
        details=(
            f"Asset {asset['asset_id']} returned{' (batch)' if batch else ''} by member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])} "
            f"— condition: {result['final_condition']}"
        ),
    )
    if result["is_disposed"]:
        log_action(
            staff_id=staff_id,
            action="disposed_asset",
            target_table="assets",
            target_id=asset["id"],
            target_name=f"{asset['asset_id']} — {asset['name']}",
            details=f"Asset {asset['asset_id']} disposed on return and removed from circulation",
        )


def return_asset_by_scan(conn, asset_code, member_barcode, staff_id, condition_in, return_remarks):
    transaction, asset, member, error = _find_open_transaction_for_scan(asset_code, member_barcode, conn)
    if error:
        return None, error

    try:
        result = _finalize_return(transaction, asset, member, staff_id, condition_in, return_remarks, conn)
    except Exception:
        conn.rollback()
        raise

    _log_return(staff_id, asset["id"], asset, member, result)

    return {
        "message": "asset returned successfully",
        "condition": result["final_condition"],
        "status": result["new_status"],
    }, None


def get_member_open_loans(conn, member_barcode):
    """Lists what a member has borrowed; zero loans is a normal 200 with an empty list, not an error."""
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


def lookup_return_scan(conn, asset_code, member_barcode):
    """Read-only per-scan lookup for the scan-return cart; nothing mutates until batch-return submits."""
    transaction, asset, _member, error = _find_open_transaction_for_scan(asset_code, member_barcode, conn)
    if error:
        return None, error

    return {
        "transaction_id": transaction["id"],
        "asset_name": asset["name"],
        "asset_barcode": asset["barcode_id"],
    }, None


def get_return_confirm_transaction(conn, transaction_id):
    """Opens the row-action return modal. Read-only lookup -- the actual
    mutation happens in return_transaction_by_id() below, on submit."""
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
    """Row-action return; skips the barcode lookup and just confirms the transaction is still open."""
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

    _log_return(staff_id, transaction["id"], asset, member, result)

    return {
        "message": f"{asset['asset_id']} returned successfully",
        "condition": result["final_condition"],
        "status": result["new_status"],
    }, None


# # # BATCH RETURN (API action) -------------------------------------------


def get_batch_return_candidates(conn, ids):
    """Silently drops ids that are missing or already returned."""
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
    """Applies condition/remarks to selected transactions; best-effort, one bad row doesn't abort the rest.
    `items` is a list of (transaction_id, condition_in) already validated by the route."""
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
        _log_return(staff_id, transaction["id"], asset, member, result, batch=True)

    return {
        "message": f"{len(returned_ids)} asset(s) returned"
        + (f", {len(skipped_ids)} skipped" if skipped_ids else ""),
        "returned": returned_ids,
        "skipped": skipped_ids,
    }, None


# # # RENEW / EXTEND DUE DATE (API action) ---------------------------------


def get_renewable_transaction(conn, transaction_id):
    """Opens the extend-due-date modal -- read-only lookup, same split as
    get_return_confirm_transaction()/return_transaction_by_id()."""
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
    """Pushes a due date out (or corrects it) without touching status."""
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
        staff_id=staff_id,
        action="renewed_loan",
        target_table="transactions",
        target_id=transaction_id,
        target_name=(
            f"{txn_asset['asset_id']} — {full_name(txn_member['first_name'], txn_member['last_name'], txn_member['middle_name'], txn_member['suffix'])}"
            if txn_asset and txn_member
            else None
        ),
        details=f"Due date extended from {old_due_date or '—'} to {new_due_date}",
    )

    return {"message": "Due date updated", "due_date": new_due_date}, None


# # # CHECKOUT SESSIONS (API action) -------------------------------------


def list_open_checkout_sessions(conn):
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
    """The actual commit point -- nothing lands in `transactions` until
    this runs; everything scanned before it is just a reservation."""
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

    # re-check eligibility at commit time, not just cart-creation -- status may have changed while scanning
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
            jsonify({"error": "Cart is empty — scan at least one asset before checking out"}),
            400,
        )

    max_loans = get_max_concurrent_loans(member["member_type"], conn)
    if current_loan_count + len(items) > max_loans:
        return None, (
            jsonify(
                {
                    "error": (
                        f"This checkout would put the member at {current_loan_count + len(items)} "
                        f"concurrent loans, exceeding the maximum of {max_loans}"
                    ),
                    "current_loans": current_loan_count,
                    "cart_size": len(items),
                }
            ),
            403,
        )

    try:
        for item in items:
            asset = conn.execute("SELECT * FROM assets WHERE id = ?", (item["asset_id"],)).fetchone()

            claim = conn.execute(
                "UPDATE assets SET status = 'borrowed' WHERE id = ? AND status = 'reserved'",
                (item["asset_id"],),
            )
            if claim.rowcount == 0:
                raise ValueError(f"asset {item['asset_id']} was not in reserved state")

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

    log_action(
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
