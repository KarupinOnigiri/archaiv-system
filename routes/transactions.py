from flask import Blueprint, jsonify, g
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required
from services.loan_service import check_and_restrict_overdue_member
from utils.logger import log_action
from utils.formatters import full_name

transactions_bp = Blueprint("transactions", __name__)


# # # LIST / VIEW ------------------------------------------------------------


@transactions_bp.route("/transactions")
@login_required
def list_transactions():
    conn = get_db_connection()
    transactions = conn.execute("""
        SELECT transactions.*, assets.name AS asset_name, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        ORDER BY transactions.time_out DESC
        """).fetchall()
    conn.close()
    return jsonify([dict(row) for row in transactions])


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
        AND transactions.due_date < CURRENT_TIMESTAMP
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
                data.get("due_date"),
                asset["condition"],
            ),
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
        details=f"Asset {asset['asset_id']} returned by member {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
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

    try:
        conn.execute(
            """
            UPDATE transactions
            SET time_in = CURRENT_TIMESTAMP, status = 'returned',
                returned_by_staff_id = ?, return_remarks = ?
            WHERE id = ?
            """,
            (g.staff["id"], data.get("return_remarks"), transaction["id"]),
        )

        conn.execute(
            "UPDATE assets SET status = 'available' WHERE id = ?", (asset["id"],)
        )

        conn.commit()
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
        details=f"Asset {asset['asset_id']} borrowed by member {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
    )

    return jsonify({"message": "asset returned successfully"}), 200


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
