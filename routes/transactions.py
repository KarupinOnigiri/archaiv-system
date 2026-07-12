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

transactions_bp = Blueprint("transactions", __name__)

DEFAULT_LOAN_DAYS = 2
MAX_CONCURRENT_LOANS = 10
SESSION_STALE_MINUTES = 20

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


@transactions_bp.route("/api/v1/transactions/borrow", methods=["POST"])
@login_required
def borrow_asset():
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = ["asset_code", "member_barcode"]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    due_date = data.get("due_date")

    error = validate_due_date(due_date)
    if error:
        return error

    if due_date is None:
        due_date = (datetime.now() + timedelta(days=DEFAULT_LOAN_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    conn = get_db_connection()

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

    member = conn.execute(
        "SELECT * FROM members WHERE barcode_id = ?", (data["member_barcode"],)
    ).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    if not member["is_active"]:
        conn.close()
        return jsonify({"error": "This Member is not active"}), 403

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

    session_id = data.get("session_id")
    if session_id is not None:
        checkout_session = conn.execute(
            "SELECT id, member_id FROM checkout_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if checkout_session is None:
            conn.close()
            return (
                jsonify(
                    {"error": "session_id does not match an existing checkout session"}
                ),
                404,
            )
        if checkout_session["member_id"] != member["id"]:
            conn.close()
            return (
                jsonify({"error": "session_id belongs to a different member"}),
                400,
            )

    try:
        claim = conn.execute(
            "UPDATE assets SET status = 'borrowed' WHERE id = ? AND status = 'available'",
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
            """
            INSERT INTO transactions (checkout_session_id, asset_id, member_id, borrowed_by_staff_id, due_date, condition_out, status)
            VALUES (?, ?, ?, ?, ?, ?, 'borrowed')
            """,
            (
                data.get("session_id"),
                asset["id"],
                member["id"],
                g.staff["id"],
                due_date,
                asset["condition"],
            ),
        )

        if session_id is not None:
            conn.execute(
                "UPDATE checkout_sessions SET completed_at = datetime('now', 'localtime') WHERE id = ? AND completed_at IS NULL",
                (session_id,),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="borrowed_asset",
        target_table="transactions",
        target_id=asset["id"],
        details=f"Asset {asset['asset_id']} borrowed by member {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
    )

    return jsonify({"message": "asset borrowed successfully"}), 201


# # # RETURN (API action) -----------------------------------------------


@transactions_bp.route("/api/v1/transactions/return", methods=["POST"])
@login_required
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

    conn.execute(
        "DELETE FROM checkout_sessions WHERE completed_at IS NULL AND created_at < datetime('now', 'localtime', ?)",
        (f"-{SESSION_STALE_MINUTES} minutes",),
    )
    conn.commit()

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
