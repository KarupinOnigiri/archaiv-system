from flask import Blueprint, jsonify, g, request
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from datetime import datetime
from utils.validators import (
    validate_date_string,
    validate_date_fields,
    parse_pagination_params,
)
from extensions import limiter
import sqlite3
import logging

logger = logging.getLogger(__name__)

assets_bp = Blueprint("assets", __name__)


@assets_bp.route("/assets")
@login_required
def view_assets():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    total_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM assets WHERE deleted_at IS NULL"
    ).fetchone()["cnt"]
    assets = conn.execute(
        "SELECT * FROM assets WHERE deleted_at IS NULL ORDER BY id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    conn.close()
    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in assets],
        }
    )


@assets_bp.route("/assets/add", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def add_asset_submit():
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = ["name", "barcode_id"]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    error = validate_date_fields(data, ("acquired_date", "last_calibrated"))

    if error:
        return error

    conn = get_db_connection()
    new_asset_id = None
    generated_code = None

    try:
        cursor = conn.execute(
            """
            INSERT INTO assets (asset_id, name, barcode_id, rfid_id, category, serial_code, acquired_date, last_calibrated, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "",
                data["name"],
                data["barcode_id"],
                data.get("rfid_id"),
                data.get("category"),
                data.get("serial_code"),
                data.get("acquired_date"),
                data.get("last_calibrated"),
                data.get("notes"),
            ),
        )
        new_asset_id = cursor.lastrowid
        generated_code = f"AST-{new_asset_id:05d}"
        conn.execute(
            "UPDATE assets SET asset_id = ? WHERE id = ?",
            (generated_code, new_asset_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        print(f"IntegrityError: {e}")
        conn.rollback()
        return (
            jsonify(
                {
                    "error": "A database constraint was violated. Check for duplicate barcode/username/ID values."
                }
            ),
            400,
        )
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="created_asset",
        target_table="assets",
        target_id=new_asset_id,
        details=f"Created asset {generated_code}",
    )

    return (
        jsonify(
            {
                "message": "Asset added successfully",
                "id": new_asset_id,
                "asset_id": generated_code,
            }
        ),
        201,
    )


@assets_bp.route("/assets/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def edit_asset_submit(id):
    data, error = get_json_or_error()
    if error:
        return error

    error = validate_date_fields(data, ("acquired_date", "last_calibrated"))

    if error:
        return error

    valid_conditions = ("functional", "inoperable", "for_repair", "disposed")

    condition = data.get("condition")
    if condition is not None and condition not in valid_conditions:
        return (
            jsonify(
                {"error": f"condition must be one of: {', '.join(valid_conditions)}"}
            ),
            400,
        )
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    new_status = data.get("status", asset["status"])

    if new_status in ("borrowed", "reserved") and asset["status"] != new_status:
        conn.close()
        return (
            jsonify(
                {
                    "error": f"Cannot set status to '{new_status}' directly",
                    "reason": "This status is only set by the checkout process, not by editing the asset.",
                }
            ),
            400,
        )

    if new_status != asset["status"] and asset["status"] in ("borrowed", "reserved"):
        conn.close()
        return (
            jsonify(
                {
                    "error": f"Cannot change status: asset is currently {asset['status']}",
                    "reason": "This asset is out with a member or in an active checkout cart. It must be returned or the cart cancelled before its status can be changed.",
                }
            ),
            409,
        )
    try:
        conn.execute(
            """
            UPDATE assets
            SET name = ?, category = ?, condition = ?, status = ?, notes = ?, acquired_date = ?, last_calibrated = ?
            WHERE id = ?
            """,
            (
                data.get("name", asset["name"]),
                data.get("category", asset["category"]),
                data.get("condition", asset["condition"]),
                new_status,
                data.get("notes", asset["notes"]),
                data.get("acquired_date", asset["acquired_date"]),
                data.get("last_calibrated", asset["last_calibrated"]),
                asset["id"],
            ),
        )
    except sqlite3.IntegrityError as e:
        conn.rollback()
        print(f"IntegrityError: {e}")
        return (
            jsonify(
                {
                    "error": "A database constraint was violated. Check for duplicate barcode/username/ID values."
                }
            ),
            400,
        )
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_asset",
        target_table="assets",
        target_id=asset["id"],
        details=f"Updated asset {asset['asset_id']}",
    )

    return jsonify({"message": "Asset updated successfully"})


@assets_bp.route("/assets/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def delete_asset(id):
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    if asset["deleted_at"] is not None:
        conn.close()
        return jsonify({"error": "Asset is already deleted"}), 400

    if asset["status"] in ("borrowed", "reserved"):
        conn.close()
        return (
            jsonify(
                {
                    "error": "Cannot delete asset: it is currently borrowed",
                    "reason": "This asset has an open borrow transaction. Return it first, or contact an admin.",
                }
            ),
            409,
        )

    deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("UPDATE assets SET deleted_at = ? WHERE id = ?", (deleted_at, id))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        print(f"IntegrityError: {e}")
        return (
            jsonify(
                {
                    "error": "A database constraint was violated. Check for duplicate barcode/username/ID values."
                }
            ),
            400,
        )
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="deleted_asset",
        target_table="assets",
        target_id=id,
        details=f"Soft-deleted asset {asset['asset_id']}",
    )

    return jsonify({"message": "Asset soft deleted successfully"})


@assets_bp.route("/api/v1/assets/barcode/<string:barcode>")
@login_required
@limiter.limit("40 per minute")
def get_asset_by_barcode(barcode):
    conn = get_db_connection()
    asset = conn.execute(
        "SELECT * FROM assets WHERE (barcode_id = ? OR rfid_id = ?) AND deleted_at IS NULL",
        (barcode, barcode),
    ).fetchone()
    conn.close()

    if asset is None:
        return jsonify({"error": "Asset not found"}), 404

    return jsonify(dict(asset))


@assets_bp.route("/assets/<int:id>/history")
@login_required
def get_asset_history(id):
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()

    asset = conn.execute("SELECT id FROM assets WHERE id = ?", (id,)).fetchone()
    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    total_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE asset_id = ?", (id,)
    ).fetchone()["cnt"]

    history = conn.execute(
        """
        SELECT transactions.*, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.asset_id = ?
        ORDER BY transactions.time_out DESC
        LIMIT ? OFFSET ?
        """,
        (id, limit, offset),
    ).fetchall()
    conn.close()

    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in history],
        }
    )
