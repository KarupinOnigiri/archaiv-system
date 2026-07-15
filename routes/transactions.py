from flask import Blueprint, jsonify, g, request
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required
from services.loan_service import (
    check_and_restrict_overdue_member,
    check_and_lift_restriction_if_resolved,
)
from utils.logger import log_action
from utils.formatters import full_name
from utils.validators import (
    validate_date_string,
    validate_due_date,
    parse_pagination_params,
)
from datetime import datetime, timedelta
from utils.constants import SESSION_STALE_MINUTES
import sqlite3
from extensions import limiter

transactions_bp = Blueprint("transactions", __name__)


DEFAULT_LOAN_DAYS = 2
MAX_CONCURRENT_LOANS = 10


def _get_active_checkout_session(session_id, member_id, conn):
    """Returns (session_row, error_response) for a checkout session that a
    borrow can be attached to.

    A checkout session represents one checkout episode where a member can
    have several assets borrowed against the same session_id (badge scanned
    once, staff scans multiple tools). It stays open until either:
      - staff explicitly finishes it (POST .../finish), or
      - it goes stale (no activity for SESSION_STALE_MINUTES), which this
        function lazily closes the moment it's noticed.

    error_response is None if the session exists, belongs to member_id,
    and is still open.
    """
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

    created_at = datetime.strptime(session["created_at"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() - created_at > timedelta(minutes=SESSION_STALE_MINUTES):
        # lazily close it so it stops showing up as "open" going forward
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


# # # LIST / VIEW ------------------------------------------------------------


@transactions_bp.route("/transactions")
@login_required
def list_transactions():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    total_count = conn.execute("SELECT COUNT(*) AS cnt FROM transactions").fetchone()[
        "cnt"
    ]
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
    conn.close()
    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in transactions],
        }
    )


@transactions_bp.route("/transactions/overdue")
@login_required
def list_overdue_transactions():
    conn = get_db_connection()
    overdue = conn.execute("""
        SELECT transactions.*, assets.name AS asset_name, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.status = 'borrowed'
        AND transactions.due_date IS NOT NULL
        AND transactions.due_date < datetime('now', 'localtime')
        ORDER BY transactions.due_date ASC
        """).fetchall()
    conn.close()
    return jsonify([dict(row) for row in overdue])


# # # BORROW (API action) -----------------------------------------------


# # # CHECKOUT SESSION ITEMS (the "cart") --------------------------------


@transactions_bp.route("/api/v1/checkout-sessions/<int:id>/items/add", methods=["POST"])
@login_required
@limiter.limit("60 per minute")
def add_item_to_session(id):
    data, error = get_json_or_error()
    if error:
        return error

    if "asset_code" not in data or not data["asset_code"]:
        return jsonify({"error": "Missing required field: asset_code"}), 400

    conn = get_db_connection()

    session = conn.execute(
        "SELECT * FROM checkout_sessions WHERE id = ?", (id,)
    ).fetchone()

    if session is None:
        conn.close()
        return jsonify({"error": "Checkout session not found"}), 404

    _, error = _get_active_checkout_session(id, session["member_id"], conn)
    if error:
        conn.close()
        return error

    asset = conn.execute(
        "SELECT * FROM assets WHERE (barcode_id = ? OR rfid_id = ?) AND deleted_at IS NULL",
        (data["asset_code"], data["asset_code"]),
    ).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    if asset["status"] != "available":
        conn.close()
        return (
            jsonify({"error": f"Asset is currently {asset['status']}, not available"}),
            400,
        )

    if asset["condition"] == "disposed":
        conn.close()
        return jsonify({"error": "asset has been disposed and cannot be borrowed"}), 400

    try:
        claim = conn.execute(
            "UPDATE assets SET status = 'reserved' WHERE id = ? AND status = 'available'",
            (asset["id"],),
        )

        if claim.rowcount == 0:
            conn.rollback()
            return (
                jsonify(
                    {
                        "error": "asset was just claimed by another transaction. Please rescan and try again."
                    }
                ),
                409,
            )

        conn.execute(
            "INSERT INTO checkout_session_items (checkout_session_id, asset_id) VALUES (?, ?)",
            (id, asset["id"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "This asset is already scanned into this cart"}), 400
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return jsonify({"message": "Asset added to cart", "asset_id": asset["id"]}), 201


@transactions_bp.route(
    "/api/v1/checkout-sessions/<int:id>/items/<int:item_id>/remove", methods=["POST"]
)
@login_required
@limiter.limit("60 per minute")
def remove_item_from_session(id, item_id):
    conn = get_db_connection()

    item = conn.execute(
        "SELECT * FROM checkout_session_items WHERE id = ? AND checkout_session_id = ?",
        (item_id, id),
    ).fetchone()

    if item is None:
        conn.close()
        return jsonify({"error": "Cart item not found"}), 404

    session = conn.execute(
        "SELECT * FROM checkout_sessions WHERE id = ?", (id,)
    ).fetchone()

    if session["status"] != "open":
        conn.close()
        return (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    try:
        conn.execute("DELETE FROM checkout_session_items WHERE id = ?", (item_id,))
        conn.execute(
            "UPDATE assets SET status = 'available' WHERE id = ? AND status = 'reserved'",
            (item["asset_id"],),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return jsonify({"message": "Item removed from cart"})


@transactions_bp.route("/api/v1/checkout-sessions/<int:id>")
@login_required
@limiter.limit("20 per minute")
def view_checkout_session(id):
    conn = get_db_connection()

    session = conn.execute(
        """
        SELECT checkout_sessions.*, members.first_name, members.last_name,
               members.middle_name, members.suffix, members.barcode_id AS member_barcode
        FROM checkout_sessions
        JOIN members ON checkout_sessions.member_id = members.id
        WHERE checkout_sessions.id = ?
        """,
        (id,),
    ).fetchone()

    if session is None:
        conn.close()
        return jsonify({"error": "Checkout session not found"}), 404

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
        (id,),
    ).fetchall()
    conn.close()

    return jsonify({**dict(session), "items": [dict(row) for row in items]})


# # # RETURN (API action) -----------------------------------------------


@transactions_bp.route("/api/v1/transactions/return", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def return_asset():
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = ["asset_code", "member_barcode"]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    condition_in = data.get("condition_in")
    valid_conditions = ("functional", "inoperable", "for_repair", "disposed")
    if condition_in is not None and condition_in not in valid_conditions:
        return (
            jsonify(
                {"error": f"condition_in must be one of: {', '.join(valid_conditions)}"}
            ),
            400,
        )

    conn = get_db_connection()

    asset = conn.execute(
        "SELECT * FROM assets WHERE (barcode_id = ? OR rfid_id = ?) AND deleted_at IS NULL",
        (data["asset_code"], data["asset_code"]),
    ).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    transaction = conn.execute(
        "SELECT * FROM transactions WHERE asset_id = ? AND status = 'borrowed'",
        (asset["id"],),
    ).fetchone()

    if transaction is None:
        conn.close()
        return jsonify({"error": "This asset has no open borrow transaction"}), 400

    member = conn.execute(
        "SELECT * FROM members WHERE barcode_id = ?", (data["member_barcode"],)
    ).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "member not found"}), 404

    if member["id"] != transaction["member_id"]:
        conn.close()
        return (
            jsonify(
                {"error": "This asset was borrowed by someone else. member mismatch."}
            ),
            403,
        )

    final_condition = condition_in if condition_in else asset["condition"]
    new_status = "available" if final_condition == "functional" else "maintenance"

    try:
        conn.execute(
            """
            UPDATE transactions
            SET time_in = datetime('now', 'localtime'), status = 'returned',
                returned_by_staff_id = ?, return_remarks = ?
            WHERE id = ?
            """,
            (g.staff["id"], data.get("return_remarks"), transaction["id"]),
        )

        conn.execute(
            "UPDATE assets SET status = ?, condition = ? WHERE id = ?",
            (new_status, final_condition, asset["id"]),
        )

        conn.commit()
        check_and_lift_restriction_if_resolved(member["id"], conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="returned_asset",
        target_table="transactions",
        target_id=asset["id"],
        details=(
            f"Asset {asset['asset_id']} returned by member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])} "
            f"— condition: {final_condition}"
        ),
    )

    return (
        jsonify(
            {
                "message": "asset returned successfully",
                "condition": final_condition,
                "status": new_status,
            }
        ),
        200,
    )


# # # CHECKOUT SESSIONS (API action) -------------------------------------


@transactions_bp.route("/api/v1/checkout-sessions", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def create_checkout_session():
    data, error = get_json_or_error()
    if error:
        return error

    if "member_barcode" not in data or not data["member_barcode"]:
        return jsonify({"error": "Missing required field: member_barcode"}), 400

    conn = get_db_connection()

    member = conn.execute(
        "SELECT * FROM members WHERE barcode_id = ?", (data["member_barcode"],)
    ).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    if not member["is_active"]:
        conn.close()
        return jsonify({"error": "This member is not active"}), 403

    if check_and_restrict_overdue_member(member["id"], conn):
        conn.close()
        return (
            jsonify(
                {
                    "error": "This member is restricted",
                    "reason": "Has an overdue asset that has not been returned",
                }
            ),
            403,
        )

    if member["is_restricted"]:
        conn.close()
        return (
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

    if current_loan_count >= MAX_CONCURRENT_LOANS:
        conn.close()
        return (
            jsonify(
                {
                    "error": f"This member has reached the maximum of {MAX_CONCURRENT_LOANS} concurrent loans",
                }
            ),
            403,
        )

    # Lazily close any of this member's own sessions that went stale, so
    # they don't linger as "open" — never hard-delete (they may already be
    # referenced by transactions via checkout_session_id).
    conn.execute(
        """
        UPDATE checkout_sessions
        SET status = 'cancelled', completed_at = datetime('now', 'localtime')
        WHERE member_id = ? AND status = 'open'
        AND created_at < datetime('now', 'localtime', ?)
        """,
        (member["id"], f"-{SESSION_STALE_MINUTES} minutes"),
    )
    conn.commit()

    # One open session per member at a time — if they still have one, send
    # staff back to it instead of forking a second concurrent session.
    existing_open_session = conn.execute(
        "SELECT id FROM checkout_sessions WHERE member_id = ? AND status = 'open'",
        (member["id"],),
    ).fetchone()

    if existing_open_session is not None:
        conn.close()
        return (
            jsonify(
                {
                    "error": "This member already has an open checkout session",
                    "session_id": existing_open_session["id"],
                }
            ),
            409,
        )

    cursor = conn.execute(
        "INSERT INTO checkout_sessions (member_id) VALUES (?)", (member["id"],)
    )
    conn.commit()
    session_id = cursor.lastrowid
    conn.close()

    return (
        jsonify({"message": "Checkout session created", "session_id": session_id}),
        201,
    )


# # # CHECKOUT SESSIONS — explicit finish -------------------------------


@transactions_bp.route("/api/v1/checkout-sessions/<int:id>/checkout", methods=["POST"])
@login_required
def checkout_session(id):
    """The actual commit point. Nothing lands in `transactions` until this
    is called — everything scanned before this is just a reservation."""
    data, error = get_json_or_error()
    if error:
        return error

    due_date = data.get("due_date")
    error = validate_due_date(due_date)
    if error:
        return error
    if due_date is None:
        due_date = (datetime.now() + timedelta(days=DEFAULT_LOAN_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    conn = get_db_connection()

    session = conn.execute(
        "SELECT * FROM checkout_sessions WHERE id = ?", (id,)
    ).fetchone()

    if session is None:
        conn.close()
        return jsonify({"error": "Checkout session not found"}), 404

    if session["status"] != "open":
        conn.close()
        return (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (session["member_id"],)
    ).fetchone()

    # Re-check eligibility at the actual moment of commitment, not just at
    # cart-creation time — status may have changed while scanning.
    if check_and_restrict_overdue_member(member["id"], conn):
        conn.close()
        return (
            jsonify(
                {
                    "error": "This member is restricted",
                    "reason": "Has an overdue asset that has not been returned",
                }
            ),
            403,
        )

    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (session["member_id"],)
    ).fetchone()
    if member["is_restricted"]:
        conn.close()
        return (
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
        "SELECT * FROM checkout_session_items WHERE checkout_session_id = ?", (id,)
    ).fetchall()

    if not items:
        conn.close()
        return (
            jsonify(
                {"error": "Cart is empty — scan at least one asset before checking out"}
            ),
            400,
        )

    if current_loan_count + len(items) > MAX_CONCURRENT_LOANS:
        conn.close()
        return (
            jsonify(
                {
                    "error": (
                        f"This checkout would put the member at {current_loan_count + len(items)} "
                        f"concurrent loans, exceeding the maximum of {MAX_CONCURRENT_LOANS}"
                    ),
                    "current_loans": current_loan_count,
                    "cart_size": len(items),
                }
            ),
            403,
        )

    try:
        for item in items:
            asset = conn.execute(
                "SELECT * FROM assets WHERE id = ?", (item["asset_id"],)
            ).fetchone()

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
                    id,
                    item["asset_id"],
                    member["id"],
                    g.staff["id"],
                    due_date,
                    asset["condition"],
                ),
            )

        conn.execute(
            "DELETE FROM checkout_session_items WHERE checkout_session_id = ?", (id,)
        )
        conn.execute(
            "UPDATE checkout_sessions SET status = 'completed', completed_at = datetime('now', 'localtime') WHERE id = ?",
            (id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="checked_out_session",
        target_table="checkout_sessions",
        target_id=id,
        details=(
            f"Checked out {len(items)} asset(s) to member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}"
        ),
    )

    return jsonify({"message": "Checkout complete", "items_borrowed": len(items)}), 201


@transactions_bp.route("/api/v1/checkout-sessions/<int:id>/cancel", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def cancel_checkout_session(id):
    conn = get_db_connection()

    session = conn.execute(
        "SELECT * FROM checkout_sessions WHERE id = ?", (id,)
    ).fetchone()

    if session is None:
        conn.close()
        return jsonify({"error": "Checkout session not found"}), 404

    if session["status"] != "open":
        conn.close()
        return (
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
            (id,),
        )
        conn.execute(
            "DELETE FROM checkout_session_items WHERE checkout_session_id = ?", (id,)
        )
        conn.execute(
            "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ?",
            (id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="cancelled_checkout_session",
        target_table="checkout_sessions",
        target_id=id,
        details=f"Cancelled checkout session {id}, released reserved assets",
    )

    return jsonify({"message": "Checkout session cancelled"})
