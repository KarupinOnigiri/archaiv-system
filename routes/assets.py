from flask import Blueprint, jsonify, g, request
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from datetime import datetime
from utils.validators import (
    validate_date_string,
    validate_date_fields,
    parse_pagination_params,
    validate_lookup_id,
)
from extensions import limiter
import sqlite3
import logging
import csv
import io

logger = logging.getLogger(__name__)

assets_bp = Blueprint("assets", __name__)


@assets_bp.route("/assets")
@login_required
def view_assets():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    where_clauses = ["assets.deleted_at IS NULL"]
    params = []

    q = request.args.get("q")
    if q:
        where_clauses.append(
            "(assets.name LIKE ? OR assets.barcode_id LIKE ? OR assets.asset_id LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like])

    category_id = request.args.get("category_id")
    if category_id:
        where_clauses.append("assets.category_id = ?")
        params.append(category_id)

    status_filter = request.args.get("status")
    if status_filter:
        where_clauses.append("assets.status = ?")
        params.append(status_filter)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM assets {where_sql}", params
    ).fetchone()["cnt"]
    assets = conn.execute(
        f"""
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        {where_sql}
        ORDER BY assets.id LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
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

    # # # CATEGORIES (lookup table) -------------------------------------------


@assets_bp.route("/categories")
@login_required
def view_categories():
    conn = get_db_connection()
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(row) for row in categories])


@assets_bp.route("/categories/add", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def add_category():
    data, error = get_json_or_error()
    if error:
        return error

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Missing required field: name"}), 400

    conn = get_db_connection()
    try:
        cursor = conn.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        conn.commit()
        new_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "A category with this name already exists"}), 400
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="created_category",
        target_table="categories",
        target_id=new_id,
        details=f"Created category: {name}",
    )

    return (
        jsonify({"message": "Category added successfully", "id": new_id, "name": name}),
        201,
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

    error = validate_lookup_id(
        conn, "categories", data.get("category_id"), "category_id"
    )
    if error:
        conn.close()
        return error

    new_asset_id = None
    generated_code = None

    try:
        cursor = conn.execute(
            """
            INSERT INTO assets (asset_id, name, barcode_id, rfid_id, category_id, serial_code, acquired_date, last_calibrated, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "",
                data["name"],
                data["barcode_id"],
                data.get("rfid_id"),
                data.get("category_id"),
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


REQUIRED_CSV_COLUMNS = ("name", "barcode_id")


OPTIONAL_CSV_COLUMNS = (
    "rfid_id",
    "category",
    "serial_code",
    "acquired_date",
    "last_calibrated",
    "notes",
)


@assets_bp.route("/assets/import", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("5 per minute")
def import_assets_csv():
    if "file" not in request.files:
        return jsonify({"error": "Missing file upload (field name: 'file')"}), 400

    file = request.files["file"]
    try:
        content = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "File must be UTF-8 encoded CSV"}), 400

    reader = csv.DictReader(io.StringIO(content))
    missing_cols = [
        c for c in REQUIRED_CSV_COLUMNS if c not in (reader.fieldnames or [])
    ]
    if missing_cols:
        return (
            jsonify(
                {"error": f"CSV missing required column(s): {', '.join(missing_cols)}"}
            ),
            400,
        )

    conn = get_db_connection()
    created, errors = [], []

    for row_num, row in enumerate(reader, start=2):  # row 1 is the header
        name = (row.get("name") or "").strip()
        barcode_id = (row.get("barcode_id") or "").strip()
        if not name or not barcode_id:
            errors.append({"row": row_num, "error": "name and barcode_id are required"})
            continue

        date_error = validate_date_fields(row, ("acquired_date", "last_calibrated"))
        if date_error:
            errors.append({"row": row_num, "error": "invalid date format"})
            continue

        try:
            category_name = (row.get("category") or "").strip()
            category_id = None
            if category_name:
                cat_row = conn.execute(
                    "SELECT id FROM categories WHERE name = ?", (category_name,)
                ).fetchone()
                if cat_row is None:
                    category_id = conn.execute(
                        "INSERT INTO categories (name) VALUES (?)", (category_name,)
                    ).lastrowid
                else:
                    category_id = cat_row["id"]

            cursor = conn.execute(
                """
                INSERT INTO assets (asset_id, name, barcode_id, rfid_id, category_id, serial_code, acquired_date, last_calibrated, notes)
                VALUES ('', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    barcode_id,
                    row.get("rfid_id") or None,
                    category_id,
                    row.get("serial_code") or None,
                    row.get("acquired_date") or None,
                    row.get("last_calibrated") or None,
                    row.get("notes") or None,
                ),
            )
            new_id = cursor.lastrowid
            conn.execute(
                "UPDATE assets SET asset_id = ? WHERE id = ?",
                (f"AST-{new_id:05d}", new_id),
            )
            conn.commit()
            created.append({"row": row_num, "id": new_id, "barcode_id": barcode_id})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            errors.append(
                {"row": row_num, "error": f"duplicate or constraint violation: {e}"}
            )

    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="bulk_imported_assets",
        target_table="assets",
        details=f"Imported {len(created)} asset(s) via CSV, {len(errors)} error(s)",
    )

    return jsonify(
        {"created_count": len(created), "created": created, "errors": errors}
    ), (201 if created else 400)


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

    if "category_id" in data:
        error = validate_lookup_id(
            conn, "categories", data.get("category_id"), "category_id"
        )
        if error:
            conn.close()
            return error

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
            SET name = ?, category_id = ?, condition = ?, status = ?, notes = ?, acquired_date = ?, last_calibrated = ?
            WHERE id = ?
            """,
            (
                data.get("name", asset["name"]),
                data.get("category_id", asset["category_id"]),
                data.get("condition", asset["condition"]),
                new_status,
                data.get("notes", asset["notes"]),
                data.get("acquired_date", asset["acquired_date"]),
                data.get("last_calibrated", asset["last_calibrated"]),
                asset["id"],
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
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
        logger.error(f"IntegrityError: {e}")
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


@assets_bp.route("/assets/barcode/<string:barcode>")
@login_required
@limiter.limit("40 per minute")
def get_asset_by_barcode(barcode):
    conn = get_db_connection()
    asset = conn.execute(
        """
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE (assets.barcode_id = ? OR assets.rfid_id = ?) AND assets.deleted_at IS NULL
        """,
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
