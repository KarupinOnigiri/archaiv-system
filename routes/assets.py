from flask import (
    Blueprint,
    jsonify,
    g,
    redirect,
    request,
    render_template,
    make_response,
)
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from datetime import datetime
from utils.validators import (
    validate_date_fields,
    parse_pagination_params,
    validate_lookup_id,
)
from utils.uploads import save_asset_image, delete_asset_image, validate_image_upload
from extensions import limiter
import sqlite3
import logging
import csv
import io
import json

logger = logging.getLogger(__name__)

assets_bp = Blueprint("assets", __name__)

ADDITIONAL_IMAGE_SLOTS = (1, 2)


def _fetch_asset_images(asset_id, conn):
    """Returns {1: row_or_None, 2: row_or_None} for the two fixed
    additional-image slots, so templates can always index by slot number
    regardless of which ones are actually filled."""
    rows = conn.execute(
        "SELECT * FROM asset_images WHERE asset_id = ?", (asset_id,)
    ).fetchall()
    by_slot = {slot: None for slot in ADDITIONAL_IMAGE_SLOTS}
    for row in rows:
        by_slot[row["slot"]] = row
    return by_slot


@assets_bp.route("/assets")
@login_required
def view_assets():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    show_archived = request.args.get("show_archived") == "1"

    if show_archived:
        where_clauses = ["assets.deleted_at IS NOT NULL"]
    else:
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
    return render_template(
        "assets/_table.html",
        assets=assets,
        total_count=total_count,
        limit=limit,
        offset=offset,
        show_archived=show_archived,
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


@assets_bp.route("/categories/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def edit_category(id):
    data, error = get_json_or_error()
    if error:
        return error

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Missing required field: name"}), 400

    conn = get_db_connection()
    category = conn.execute("SELECT * FROM categories WHERE id = ?", (id,)).fetchone()
    if category is None:
        conn.close()
        return jsonify({"error": "Category not found"}), 404

    try:
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "A category with this name already exists"}), 400
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="renamed_category",
        target_table="categories",
        target_id=id,
        details=f"Renamed category '{category['name']}' to '{name}'",
    )

    return jsonify({"message": "Category renamed successfully"})


@assets_bp.route("/assets/add-form")
@roles_required("admin", "in_charge")
def add_asset_form():
    conn = get_db_connection()
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return render_template("assets/_add_modal.html", categories=categories, error=None)


@assets_bp.route("/assets/add", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def add_asset_submit():
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    required_fields = ["name", "barcode_id"]
    for field in required_fields:
        if field not in data or not data[field]:
            error_msg = f"Missing required field: {field}"
            if request.is_json:
                return jsonify({"error": error_msg}), 200
            conn = get_db_connection()
            categories = conn.execute(
                "SELECT * FROM categories ORDER BY name"
            ).fetchall()
            conn.close()
            return (
                render_template(
                    "assets/_add_modal.html", categories=categories, error=error_msg
                ),
                200,
            )

    error = validate_date_fields(data, ("acquired_date", "last_calibrated"))
    if error:
        return error

    # Image is optional on add. Validate it up front (before touching the
    # DB) so a bad file never leaves a half-created asset behind.
    image_file = request.files.get("image") if request.files else None
    has_image = image_file is not None and image_file.filename != ""

    if has_image:
        img_error = validate_image_upload(image_file)
        if img_error:
            if request.is_json:
                return jsonify({"error": img_error}), 200
            conn = get_db_connection()
            categories = conn.execute(
                "SELECT * FROM categories ORDER BY name"
            ).fetchall()
            conn.close()
            return (
                render_template(
                    "assets/_add_modal.html", categories=categories, error=img_error
                ),
                200,
            )

    # Additional images (optional, up to 2 fixed slots) -- validated
    # up front for the same reason as the main image: never leave a
    # half-created asset behind because a later image turned out invalid.
    additional_image_files = {}
    for slot in ADDITIONAL_IMAGE_SLOTS:
        file = request.files.get(f"additional_image_{slot}") if request.files else None
        if file is not None and file.filename != "":
            img_error = validate_image_upload(file)
            if img_error:
                if request.is_json:
                    return jsonify({"error": img_error}), 200
                conn = get_db_connection()
                categories = conn.execute(
                    "SELECT * FROM categories ORDER BY name"
                ).fetchall()
                conn.close()
                return (
                    render_template(
                        "assets/_add_modal.html", categories=categories, error=img_error
                    ),
                    200,
                )
            additional_image_files[slot] = file

    conn = get_db_connection()

    error = validate_lookup_id(
        conn, "categories", data.get("category_id") or None, "category_id"
    )
    if error:
        conn.close()
        return error

    new_asset_id = None
    generated_code = None
    saved_image_path = None
    saved_additional_paths = {}

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
                data.get("rfid_id") or None,
                data.get("category_id") or None,
                data.get("serial_code") or None,
                data.get("acquired_date") or None,
                data.get("last_calibrated") or None,
                data.get("notes") or None,
            ),
        )
        new_asset_id = cursor.lastrowid
        generated_code = f"AST-{new_asset_id:05d}"
        conn.execute(
            "UPDATE assets SET asset_id = ? WHERE id = ?",
            (generated_code, new_asset_id),
        )

        if has_image:
            saved_image_path, img_error = save_asset_image(image_file, generated_code)
            if img_error:
                # Asset row would otherwise be created without the image the
                # staff member explicitly attached — roll back the whole
                # submission instead of silently dropping it.
                conn.rollback()
                if request.is_json:
                    return jsonify({"error": img_error}), 200
                conn2 = get_db_connection()
                categories = conn2.execute(
                    "SELECT * FROM categories ORDER BY name"
                ).fetchall()
                conn2.close()
                return (
                    render_template(
                        "assets/_add_modal.html", categories=categories, error=img_error
                    ),
                    200,
                )
            conn.execute(
                "UPDATE assets SET image_path = ? WHERE id = ?",
                (saved_image_path, new_asset_id),
            )

        for slot, file in additional_image_files.items():
            saved_path, img_error = save_asset_image(file, f"{generated_code}-alt{slot}")
            if img_error:
                # Same reasoning as the main image: don't leave the asset
                # half-created with only some of its attached images.
                conn.rollback()
                if saved_image_path:
                    delete_asset_image(saved_image_path)
                for p in saved_additional_paths.values():
                    delete_asset_image(p)
                if request.is_json:
                    return jsonify({"error": img_error}), 200
                conn2 = get_db_connection()
                categories = conn2.execute(
                    "SELECT * FROM categories ORDER BY name"
                ).fetchall()
                conn2.close()
                return (
                    render_template(
                        "assets/_add_modal.html", categories=categories, error=img_error
                    ),
                    200,
                )
            saved_additional_paths[slot] = saved_path
            conn.execute(
                "INSERT INTO asset_images (asset_id, slot, image_path) VALUES (?, ?, ?)",
                (new_asset_id, slot, saved_path),
            )

        conn.commit()
    except sqlite3.IntegrityError as e:
        print(f"IntegrityError: {e}")
        conn.rollback()
        if saved_image_path:
            delete_asset_image(saved_image_path)
        for p in saved_additional_paths.values():
            delete_asset_image(p)
        error_msg = "A database constraint was violated. Check for duplicate barcode/serial/ID values."
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        conn2 = get_db_connection()
        categories = conn2.execute("SELECT * FROM categories ORDER BY name").fetchall()
        conn2.close()
        return (
            render_template(
                "assets/_add_modal.html", categories=categories, error=error_msg
            ),
            200,
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

    if request.is_json:
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
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/assets"
    return response


REQUIRED_CSV_COLUMNS = ("name", "barcode_id")


OPTIONAL_CSV_COLUMNS = (
    "rfid_id",
    "category",
    "serial_code",
    "acquired_date",
    "last_calibrated",
    "notes",
)


@assets_bp.route("/assets/import-form")
@roles_required("admin", "in_charge")
def import_assets_form():
    return render_template("assets/_import_modal.html", error=None)


def _import_error_response(error_msg, status):
    """Mirrors the other _*_error_response helpers: JSON for API callers,
    the modal re-rendered with an inline error for htmx/form callers."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    return render_template("assets/_import_modal.html", error=error_msg), 200


@assets_bp.route("/assets/import", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("5 per minute")
def import_assets_csv():
    if "file" not in request.files:
        return _import_error_response("Missing file upload (field name: 'file')", 400)

    file = request.files["file"]
    if not file.filename:
        return _import_error_response("No file selected", 400)

    try:
        content = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return _import_error_response("File must be UTF-8 encoded CSV", 400)

    reader = csv.DictReader(io.StringIO(content))
    missing_cols = [
        c for c in REQUIRED_CSV_COLUMNS if c not in (reader.fieldnames or [])
    ]
    if missing_cols:
        return _import_error_response(
            f"CSV missing required column(s): {', '.join(missing_cols)}", 400
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

    if request.is_json:
        return jsonify(
            {"created_count": len(created), "created": created, "errors": errors}
        ), (201 if created else 400)

    response = make_response(
        render_template(
            "assets/_import_results.html",
            created_count=len(created),
            created=created,
            errors=errors,
        )
    )
    if created:
        response.headers["HX-Trigger"] = "assets-refreshed"
    return response


@assets_bp.route("/assets/<int:id>")
@login_required
def get_asset(id):
    conn = get_db_connection()
    asset = conn.execute(
        """
        SELECT assets.*, categories.name AS category_name
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE assets.id = ? AND assets.deleted_at IS NULL
        """,
        (id,),
    ).fetchone()
    conn.close()

    if asset is None:
        return jsonify({"error": "Asset not found"}), 404

    return jsonify(dict(asset))


@assets_bp.route("/assets/<int:id>/edit-form")
@roles_required("admin", "in_charge")
def edit_asset_form(id):
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
    if asset is None:
        conn.close()
        if request.is_json:
            return jsonify({"error": "Asset not found"}), 404
        return jsonify({"error": "Asset not found"}), 404
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    additional_images = _fetch_asset_images(id, conn)
    conn.close()
    return render_template(
        "assets/_edit_modal.html",
        asset=asset,
        categories=categories,
        additional_images=additional_images,
        error=None,
    )


def _edit_error_response(
    payload_or_msg, status, asset=None, categories=None, asset_id=None
):
    """Returns JSON if this was a JSON request; renders the edit modal
    with an inline error if it was an htmx/form request."""
    if request.is_json:
        return jsonify(payload_or_msg), status
    conn = get_db_connection()
    if asset is None:
        asset = conn.execute(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
    if categories is None:
        categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    additional_images = _fetch_asset_images(asset["id"], conn)
    conn.close()
    error_msg = (
        payload_or_msg.get("error")
        if isinstance(payload_or_msg, dict)
        else payload_or_msg
    )
    return (
        render_template(
            "assets/_edit_modal.html",
            asset=asset,
            categories=categories,
            additional_images=additional_images,
            error=error_msg,
        ),
        200,
    )


@assets_bp.route("/assets/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def edit_asset_submit(id):
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    error = validate_date_fields(data, ("acquired_date", "last_calibrated"))
    if error:
        return error

    valid_conditions = ("functional", "inoperable", "for_repair", "disposed")
    condition = data.get("condition")
    if condition is not None and condition not in valid_conditions:
        return _edit_error_response(
            {"error": f"condition must be one of: {', '.join(valid_conditions)}"},
            400,
            asset_id=id,
        )

    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()

    if asset is None:
        conn.close()
        if request.is_json:
            return jsonify({"error": "Asset not found"}), 404
        return jsonify({"error": "Asset not found"}), 404

    if asset["deleted_at"] is not None:
        conn.close()
        return _edit_error_response(
            {
                "error": "Cannot edit a deleted asset. Restore it first via /assets/<id>/restore."
            },
            400,
            asset=asset,
            asset_id=id,
        )

    if "category_id" in data:
        error = validate_lookup_id(
            conn, "categories", data.get("category_id"), "category_id"
        )
        if error:
            conn.close()
            return error

    # Image: three possible outcomes — replace with a new file, remove the
    # existing one, or leave it untouched (the default/common case).
    image_file = request.files.get("image") if request.files else None
    has_new_image = image_file is not None and image_file.filename != ""
    remove_image = str(data.get("remove_image", "0")) == "1"

    new_image_path = asset["image_path"]
    old_image_to_delete = None

    if has_new_image:
        img_error = validate_image_upload(image_file)
        if img_error:
            conn.close()
            return _edit_error_response(
                {"error": img_error}, 400, asset=asset, asset_id=id
            )
    elif remove_image and asset["image_path"]:
        old_image_to_delete = asset["image_path"]
        new_image_path = None

    # Additional images: same three outcomes as the main image, but
    # per fixed slot (1, 2) -- replace, remove, or leave untouched.
    existing_additional_images = _fetch_asset_images(id, conn)
    additional_actions = {}

    for slot in ADDITIONAL_IMAGE_SLOTS:
        file = request.files.get(f"additional_image_{slot}") if request.files else None
        has_new = file is not None and file.filename != ""
        remove_flag = str(data.get(f"remove_additional_image_{slot}", "0")) == "1"
        existing_row = existing_additional_images.get(slot)

        if has_new:
            img_error = validate_image_upload(file)
            if img_error:
                conn.close()
                return _edit_error_response(
                    {"error": img_error}, 400, asset=asset, asset_id=id
                )
            additional_actions[slot] = {
                "action": "replace",
                "file": file,
                "old_path": existing_row["image_path"] if existing_row else None,
            }
        elif remove_flag and existing_row is not None:
            additional_actions[slot] = {
                "action": "remove",
                "old_path": existing_row["image_path"],
            }

    # Reorder: swap the main image with an already-saved additional-image
    # slot (the "Set as Main" button in the edit modal). Only applies when
    # neither side of the swap is also being independently replaced/removed
    # in this same submit -- an explicit replace/remove always wins, and the
    # frontend disables the swap controls in that case so this should only
    # ever be reached with a clean swap request.
    swap_requested = str(data.get("swap_with_slot", "")).strip()
    if (
        swap_requested in ("1", "2")
        and not has_new_image
        and not remove_image
        and int(swap_requested) not in additional_actions
    ):
        swap_slot = int(swap_requested)
        swap_existing_row = existing_additional_images.get(swap_slot)
        old_main_path = asset["image_path"]
        new_image_path = swap_existing_row["image_path"] if swap_existing_row else None
        additional_actions[swap_slot] = {"action": "swap", "new_path": old_main_path}

    new_condition = data.get("condition", asset["condition"])

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

    if new_condition == "disposed" and asset["status"] in ("borrowed", "reserved"):
        conn.close()
        return (
            jsonify(
                {
                    "error": "Cannot dispose asset: it is currently borrowed",
                    "reason": "This asset has an open borrow transaction. Return it first, or contact an admin.",
                }
            ),
            409,
        )

    saved_additional_paths = {}
    old_additional_to_delete = []

    try:
        if has_new_image:
            # Reuses the asset's existing code as the filename prefix — a
            # fresh random suffix is still appended inside save_asset_image,
            # so this never collides with (or overwrites) the old file.
            saved_path, img_error = save_asset_image(image_file, asset["asset_id"])
            if img_error:
                return _edit_error_response(
                    {"error": img_error}, 400, asset=asset, asset_id=id
                )
            old_image_to_delete = asset["image_path"]
            new_image_path = saved_path

        for slot, act in additional_actions.items():
            if act["action"] == "replace":
                saved_path, img_error = save_asset_image(
                    act["file"], f"{asset['asset_id']}-alt{slot}"
                )
                if img_error:
                    return _edit_error_response(
                        {"error": img_error}, 400, asset=asset, asset_id=id
                    )
                saved_additional_paths[slot] = saved_path
                if act["old_path"]:
                    old_additional_to_delete.append(act["old_path"])
                conn.execute(
                    """
                    INSERT INTO asset_images (asset_id, slot, image_path) VALUES (?, ?, ?)
                    ON CONFLICT(asset_id, slot) DO UPDATE SET image_path = excluded.image_path
                    """,
                    (id, slot, saved_path),
                )
            elif act["action"] == "remove":
                old_additional_to_delete.append(act["old_path"])
                conn.execute(
                    "DELETE FROM asset_images WHERE asset_id = ? AND slot = ?",
                    (id, slot),
                )
            elif act["action"] == "swap":
                # Just moving existing pointers between assets.image_path
                # and asset_images.slot -- nothing was replaced or discarded,
                # so no file gets queued for deletion here.
                if act["new_path"]:
                    conn.execute(
                        """
                        INSERT INTO asset_images (asset_id, slot, image_path) VALUES (?, ?, ?)
                        ON CONFLICT(asset_id, slot) DO UPDATE SET image_path = excluded.image_path
                        """,
                        (id, slot, act["new_path"]),
                    )
                else:
                    conn.execute(
                        "DELETE FROM asset_images WHERE asset_id = ? AND slot = ?",
                        (id, slot),
                    )

        if new_condition == "disposed":
            conn.execute(
                """
                UPDATE assets
                SET name = ?, barcode_id = ?, rfid_id = ?, category_id = ?, condition = ?, status = ?, notes = ?, acquired_date = ?, last_calibrated = ?, image_path = ?, deleted_at = datetime('now', 'localtime')
                WHERE id = ?
                """,
                (
                    data.get("name", asset["name"]),
                    data.get("barcode_id", asset["barcode_id"]),
                    data.get("rfid_id", asset["rfid_id"]) or None,
                    data.get("category_id", asset["category_id"]),
                    new_condition,
                    new_status,
                    data.get("notes", asset["notes"]),
                    data.get("acquired_date", asset["acquired_date"]),
                    data.get("last_calibrated", asset["last_calibrated"]),
                    new_image_path,
                    asset["id"],
                ),
            )
        else:
            conn.execute(
                """
                UPDATE assets
                SET name = ?, barcode_id = ?, rfid_id = ?, category_id = ?, condition = ?, status = ?, notes = ?, acquired_date = ?, last_calibrated = ?, image_path = ?
                WHERE id = ?
                """,
                (
                    data.get("name", asset["name"]),
                    data.get("barcode_id", asset["barcode_id"]),
                    data.get("rfid_id", asset["rfid_id"]) or None,
                    data.get("category_id", asset["category_id"]),
                    new_condition,
                    new_status,
                    data.get("notes", asset["notes"]),
                    data.get("acquired_date", asset["acquired_date"]),
                    data.get("last_calibrated", asset["last_calibrated"]),
                    new_image_path,
                    asset["id"],
                ),
            )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        if has_new_image and new_image_path:
            # The row insert/update failed after the file was already
            # written to disk — clean it up rather than leaving an orphan.
            delete_asset_image(new_image_path)
        for p in saved_additional_paths.values():
            delete_asset_image(p)
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

    if old_image_to_delete:
        # Only reached after a successful commit — safe to remove the
        # replaced/removed file now that the DB no longer references it.
        delete_asset_image(old_image_to_delete)

    for p in old_additional_to_delete:
        delete_asset_image(p)

    log_action(
        staff_id=g.staff["id"],
        action="updated_asset",
        target_table="assets",
        target_id=asset["id"],
        details=f"Updated asset {asset['asset_id']}",
    )

    if new_condition == "disposed":
        log_action(
            staff_id=g.staff["id"],
            action="disposed_asset",
            target_table="assets",
            target_id=asset["id"],
            details=f"Asset {asset['asset_id']} disposed on return and removed from circulation",
        )

    if request.is_json:
        return jsonify({"message": "Asset updated successfully"})
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/assets"
    return response


@assets_bp.route("/assets/<int:id>/delete-confirm")
@roles_required("admin", "in_charge")
def delete_asset_confirm(id):
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
    conn.close()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    return render_template("assets/_delete_modal.html", asset=asset, error=None)


@assets_bp.route("/assets/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def delete_asset(id):
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()

    if asset is None:
        conn.close()
        if request.is_json:
            return jsonify({"error": "Asset not found"}), 404
        return jsonify({"error": "Asset not found"}), 404

    if asset["deleted_at"] is not None:
        conn.close()
        error_msg = "Asset is already deleted"
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template("assets/_delete_modal.html", asset=asset, error=error_msg),
            200,
        )

    if asset["status"] in ("borrowed", "reserved"):
        conn.close()
        error_msg = "Cannot delete asset: it is currently borrowed. Return it first, or contact an admin."
        if request.is_json:
            return (
                jsonify(
                    {
                        "error": "Cannot delete asset: it is currently borrowed",
                        "reason": "This asset has an open borrow transaction. Return it first, or contact an admin.",
                    }
                ),
                409,
            )
        return (
            render_template("assets/_delete_modal.html", asset=asset, error=error_msg),
            200,
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

    if request.is_json:
        return jsonify({"message": "Asset soft deleted successfully"})
    response = make_response("", 200)
    response.headers["HX-Trigger"] = json.dumps(
        {
            "assets-refreshed": True,
            "show-toast": {
                "message": f"{asset['asset_id']} deleted successfully",
                "type": "success",
                "action": {
                    "label": "Undo",
                    "url": f"/api/v1/assets/{id}/undo-delete",
                    "method": "POST",
                    "successMessage": "Delete undone",
                    "refreshTarget": "#asset-table-container",
                },
            },
        }
    )
    return response


@assets_bp.route("/assets/<int:id>/undo-delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def undo_delete_asset(id):
    """Powers the Undo button on the delete toast. Deliberately separate
    from restore_asset even though the DB change is the same:

    - Permissions: delete is admin/in_charge, but restore is admin-only
      (reversing a possibly-stale, long-archived item is a bigger call
      than making one). Undoing your own five-second-old delete isn't
      that -- it should be available to whoever could delete in the
      first place, or an in_charge staffer's Undo button would silently
      fail against an admin-only endpoint.
    - No condition prompt: restore_asset requires picking a new condition
      when the asset's condition is 'disposed', since a long-archived
      item's condition may need re-assessing. A freshly soft-deleted
      asset's condition was never touched by delete, so there's nothing
      to re-assess -- this always restores with the condition unchanged.
    """
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    if asset["deleted_at"] is None:
        conn.close()
        return jsonify({"error": "Asset is not deleted"}), 400

    try:
        conn.execute(
            "UPDATE assets SET deleted_at = NULL, status = 'available' WHERE id = ?",
            (id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="undid_asset_delete",
        target_table="assets",
        target_id=id,
        details=f"Undid delete of asset {asset['asset_id']}",
    )

    return jsonify({"message": "Delete undone"})


@assets_bp.route("/assets/bulk-delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("10 per minute")
def bulk_delete_assets():
    """Soft-deletes multiple assets in one request. Unlike delete_asset,
    a single problem row (not found / already deleted / checked out)
    doesn't fail the whole batch -- it's recorded in `skipped` and the
    rest proceed, since the caller is a multi-select UI where "some of
    these are checked out" is an expected, not exceptional, outcome."""
    data, error = get_json_or_error()
    if error:
        return error

    raw_ids = data.get("ids")
    if not raw_ids or not isinstance(raw_ids, list):
        return (
            jsonify({"error": "Missing required field: ids (must be a non-empty list)"}),
            400,
        )

    try:
        ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify({"error": "ids must be a list of integers"}), 400

    conn = get_db_connection()
    deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    deleted, skipped = [], []

    for asset_id in ids:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

        if asset is None:
            skipped.append({"id": asset_id, "reason": "Asset not found"})
            continue

        if asset["deleted_at"] is not None:
            skipped.append(
                {"id": asset_id, "asset_id": asset["asset_id"], "reason": "Already deleted"}
            )
            continue

        if asset["status"] in ("borrowed", "reserved"):
            skipped.append(
                {
                    "id": asset_id,
                    "asset_id": asset["asset_id"],
                    "reason": "Currently borrowed or reserved",
                }
            )
            continue

        try:
            conn.execute(
                "UPDATE assets SET deleted_at = ? WHERE id = ?", (deleted_at, asset_id)
            )
            conn.commit()
            deleted.append({"id": asset_id, "asset_id": asset["asset_id"]})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk delete of asset {asset_id}: {e}")
            skipped.append(
                {
                    "id": asset_id,
                    "asset_id": asset["asset_id"],
                    "reason": "A database constraint was violated",
                }
            )

    conn.close()

    if deleted:
        log_action(
            staff_id=g.staff["id"],
            action="bulk_deleted_assets",
            target_table="assets",
            details=(
                f"Bulk soft-deleted {len(deleted)} asset(s): "
                + ", ".join(a["asset_id"] for a in deleted)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return jsonify(
        {"deleted_count": len(deleted), "deleted": deleted, "skipped": skipped}
    )


@assets_bp.route("/assets/bulk-restore", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def bulk_restore_assets():
    """Restores multiple archived assets in one request. Mirrors
    bulk_delete_assets's shape (deleted/skipped -> restored/skipped), with
    one deliberate difference: admin-only, not admin/in_charge, matching
    the single-item restore_asset route -- reversing a disposal is a
    bigger call than making one.

    Assets whose condition is 'disposed' are always skipped here rather
    than restored with their old condition, since picking a replacement
    condition needs the single-item restore modal's dropdown -- there's
    no per-row UI for that in a bulk action."""
    data, error = get_json_or_error()
    if error:
        return error

    raw_ids = data.get("ids")
    if not raw_ids or not isinstance(raw_ids, list):
        return (
            jsonify({"error": "Missing required field: ids (must be a non-empty list)"}),
            400,
        )

    try:
        ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify({"error": "ids must be a list of integers"}), 400

    conn = get_db_connection()
    restored, skipped = [], []

    for asset_id in ids:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

        if asset is None:
            skipped.append({"id": asset_id, "reason": "Asset not found"})
            continue

        if asset["deleted_at"] is None:
            skipped.append(
                {"id": asset_id, "asset_id": asset["asset_id"], "reason": "Not archived"}
            )
            continue

        if asset["condition"] == "disposed":
            skipped.append(
                {
                    "id": asset_id,
                    "asset_id": asset["asset_id"],
                    "reason": "Disposed -- restore individually to choose a new condition",
                }
            )
            continue

        try:
            conn.execute(
                "UPDATE assets SET deleted_at = NULL, status = 'available' WHERE id = ?",
                (asset_id,),
            )
            conn.commit()
            restored.append({"id": asset_id, "asset_id": asset["asset_id"]})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk restore of asset {asset_id}: {e}")
            skipped.append(
                {
                    "id": asset_id,
                    "asset_id": asset["asset_id"],
                    "reason": "A database constraint was violated",
                }
            )

    conn.close()

    if restored:
        log_action(
            staff_id=g.staff["id"],
            action="bulk_restored_assets",
            target_table="assets",
            details=(
                f"Bulk restored {len(restored)} asset(s): "
                + ", ".join(a["asset_id"] for a in restored)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return jsonify(
        {"restored_count": len(restored), "restored": restored, "skipped": skipped}
    )


@assets_bp.route("/assets/<int:id>/restore-confirm")
@roles_required("admin")
def restore_asset_confirm(id):
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
    conn.close()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    if asset["deleted_at"] is None:
        return jsonify({"error": "Asset is not deleted"}), 400
    return render_template("assets/_restore_modal.html", asset=asset, error=None)


def _restore_error_response(error_msg, status, asset_id):
    """Mirrors _edit_error_response: JSON for API callers, a re-rendered
    modal with the error inline for htmx/form callers."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    conn.close()
    return render_template("assets/_restore_modal.html", asset=asset, error=error_msg), 200


@assets_bp.route("/assets/<int:id>/restore", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def restore_asset(id):
    """Admin-only, deliberately tighter than delete_asset (admin/in_charge)
    -- reversing a disposal is a bigger call than making one."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    condition = data.get("condition") or None

    valid_conditions = ("functional", "inoperable", "for_repair", "disposed")
    if condition is not None and condition not in valid_conditions:
        return _restore_error_response(
            f"condition must be one of: {', '.join(valid_conditions)}", 400, id
        )
    if condition == "disposed":
        return _restore_error_response(
            "Cannot restore an asset with condition 'disposed' -- choose a different condition",
            400,
            id,
        )

    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    if asset["deleted_at"] is None:
        conn.close()
        return _restore_error_response("Asset is not deleted", 400, id)

    if asset["condition"] == "disposed" and condition is None:
        conn.close()
        return _restore_error_response(
            "This asset's condition is 'disposed' -- choose a new condition to restore it",
            400,
            id,
        )

    new_condition = condition if condition is not None else asset["condition"]

    try:
        conn.execute(
            "UPDATE assets SET deleted_at = NULL, condition = ?, status = 'available' WHERE id = ?",
            (new_condition, id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="restored_asset",
        target_table="assets",
        target_id=id,
        details=f"Restored asset {asset['asset_id']} (condition set to '{new_condition}')",
    )

    if request.is_json:
        return jsonify({"message": "Asset restored successfully"})
    response = make_response("", 200)
    response.headers["HX-Trigger"] = json.dumps(
        {
            "assets-refreshed": True,
            "show-toast": {
                "message": f"{asset['asset_id']} restored successfully",
                "type": "success",
                "action": {
                    "label": "Undo",
                    # Re-archiving is the natural inverse of this route.
                    # Permission-compatible too: restore is admin-only and
                    # delete is admin/in_charge, so anyone who could reach
                    # this restore toast can also hit this endpoint.
                    "url": f"/api/v1/assets/{id}/delete",
                    "method": "POST",
                    "successMessage": "Restore undone",
                },
            },
        }
    )
    return response
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


def _fetch_asset_history(asset_id, limit, offset, conn):
    """Shared by the JSON history endpoint and the history modal, so the
    two never drift out of sync with each other."""
    total_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE asset_id = ?", (asset_id,)
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
        (asset_id, limit, offset),
    ).fetchall()
    return total_count, history


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

    total_count, history = _fetch_asset_history(id, limit, offset, conn)
    conn.close()

    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in history],
        }
    )


@assets_bp.route("/assets/<int:id>/history-modal")
@login_required
def asset_history_modal(id):
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()

    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    total_count, history = _fetch_asset_history(id, limit, offset, conn)
    conn.close()

    return render_template(
        "assets/_history_modal.html",
        asset=asset,
        history=history,
        total_count=total_count,
        limit=limit,
        offset=offset,
    )


# # # DETAILS (read-only, opened by clicking a row) --------------------------

DETAILS_RECENT_ACTIVITY_LIMIT = 3


@assets_bp.route("/assets/<int:id>/details-modal")
@login_required
def asset_details_modal(id):
    """Read-only snapshot for the row-click 'Details' modal. Deliberately
    separate from edit-form (which is the mutation form) and from
    history-modal (which is the full paginated history) -- this reuses
    _fetch_asset_history for a capped 3-row teaser instead of duplicating
    that query, and adds the current borrower/due date when the asset is
    out, which neither of the other two modals shows."""
    conn = get_db_connection()

    asset = conn.execute(
        """
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE assets.id = ?
        """,
        (id,),
    ).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    current_loan = None
    if asset["status"] == "borrowed":
        current_loan = conn.execute(
            """
            SELECT transactions.*, members.first_name, members.last_name, members.middle_name, members.suffix
            FROM transactions
            JOIN members ON transactions.member_id = members.id
            WHERE transactions.asset_id = ? AND transactions.status = 'borrowed'
            ORDER BY transactions.time_out DESC
            LIMIT 1
            """,
            (id,),
        ).fetchone()

    history_total_count, recent_history = _fetch_asset_history(
        id, DETAILS_RECENT_ACTIVITY_LIMIT, 0, conn
    )
    additional_images = _fetch_asset_images(id, conn)
    conn.close()

    return render_template(
        "assets/_details_modal.html",
        asset=asset,
        current_loan=current_loan,
        recent_history=recent_history,
        history_total_count=history_total_count,
        additional_images=additional_images,
    )
