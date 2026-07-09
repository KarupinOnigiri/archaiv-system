from flask import Blueprint, jsonify, g
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from datetime import datetime
from utils.validators import validate_date_string
import sqlite3

assets_bp = Blueprint("assets", __name__)


@assets_bp.route("/assets")
@login_required
def view_assets():
    conn = get_db_connection()
    assets = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL").fetchall()
    conn.close()
    return jsonify([dict(row) for row in assets])


@assets_bp.route("/assets/add", methods=["POST"])
@roles_required("admin", "in_charge")
def add_asset_submit():
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = ["asset_id", "name", "barcode_id"]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    conn = get_db_connection()
    new_asset_id = None

    try:
        cursor = conn.execute(
            """
            INSERT INTO assets (asset_id, name, barcode_id, rfid_id, category, acquired_date, last_calibrated, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["asset_id"],
                data["name"],
                data["barcode_id"],
                data.get("rfid_id"),
                data.get("category"),
                data.get("acquired_date"),
                data.get("last_calibrated"),
                data.get("notes"),
            ),
        )
        conn.commit()
        new_asset_id = cursor.lastrowid
    except sqlite3.IntegrityError as e:
        conn.close()
        return jsonify({"error": f"Database error: {str(e)}"}), 400

    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="created_asset",
        target_table="assets",
        target_id=new_asset_id,
        details=f"Created asset {data['asset_id']}",
    )

    return jsonify({"message": "Asset added successfully", "id": new_asset_id}), 201


@assets_bp.route("/assets/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
def edit_asset_submit(id):
    data, error = get_json_or_error()
    if error:
        return error

    for field in ("acquired_date", "last_calibrated"):
        if field in data:
            error = validate_date_string(data.get(field), field)
            if error:
                return error

    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    try:
        conn.execute(
            """
            UPDATE assets
            SET name = ?, category = ?, condition = ?, notes = ?, acquired_date = ?, last_calibrated = ?
            WHERE id = ?
            """,
            (
                data.get("name", asset["name"]),
                data.get("category", asset["category"]),
                data.get("condition", asset["condition"]),
                data.get("notes", asset["notes"]),
                data.get("acquired_date", asset["acquired_date"]),
                data.get("last_calibrated", asset["last_calibrated"]),
                asset["id"],
            ),
        )
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return jsonify({"error": f"Database error: {str(e)}"}), 400
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
def delete_asset(id):
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("UPDATE assets SET deleted_at = ? WHERE id = ?", (deleted_at, id))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return jsonify({"error": f"Database error: {str(e)}"}), 400
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
