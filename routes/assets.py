from flask import (
    Blueprint,
    jsonify,
    g,
    redirect,
    request,
    render_template,
    make_response,
    current_app,
)
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from services.settings_service import is_feature_enabled, get_feature_label
from utils.logger import log_action
from datetime import datetime
from utils.validators import (
    validate_date_fields,
    validate_date_string,
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
import re
import os
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

assets_bp = Blueprint("assets", __name__)


@assets_bp.before_request
def _block_if_assets_disabled():
    """Blocks every route in this blueprint when the Inventory tab is disabled.
    Returns JSON 403 instead of a redirect since every route here returns
    JSON/htmx fragments, never a full page."""
    if not is_feature_enabled("assets"):
        return jsonify({"error": f"{get_feature_label('assets')} is currently disabled"}), 403

ADDITIONAL_IMAGE_SLOTS = (1, 2)


def _ensure_category_sort_order_column():
    """One-time idempotent migration adding sort_order to categories (mirrors
    departments) so drag/arrow reordering has somewhere to persist to.
    Safe to call every startup -- no-ops if the column already exists.
    Backfills existing rows in their current alphabetical order so nothing
    visually jumps until someone actually reorders."""
    conn = get_db_connection()
    try:
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(categories)").fetchall()}
        if "sort_order" not in existing_cols:
            conn.execute("ALTER TABLE categories ADD COLUMN sort_order INTEGER")
            rows = conn.execute("SELECT id FROM categories ORDER BY name").fetchall()
            for position, row in enumerate(rows):
                conn.execute(
                    "UPDATE categories SET sort_order = ? WHERE id = ?", (position, row["id"])
                )
            conn.commit()
            logger.info("Added categories.sort_order and backfilled %d row(s)", len(rows))
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Failed to ensure categories.sort_order column")
    finally:
        conn.close()


_ensure_category_sort_order_column()


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


SORTABLE_COLUMNS = {
    "asset_id": "assets.asset_id",
    "name": "assets.name",
    "category": "categories.name",
    "status": "assets.status",
    "deleted_at": "assets.deleted_at",
    "condition": "assets.condition",
    "barcode_id": "assets.barcode_id",
    "created_at": "assets.created_at",
}


def get_assets_table_context(args):
    """Builds everything _table.html needs, shared by view_assets() (htmx
    partial) and pages.py's assets_page() so they can't drift apart on what
    the template requires -- this used to bite us when pages.py forgot to
    pass 'offset'.

    Returns (context_dict, None) on success, or (None, error_response) on a
    bad query param."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    show_archived = args.get("show_archived") == "1"

    if show_archived:
        where_clauses = ["assets.deleted_at IS NOT NULL"]
    else:
        where_clauses = ["assets.deleted_at IS NULL"]
    params = []

    q = args.get("q")
    if q:
        where_clauses.append(
            "(assets.name LIKE ? OR assets.barcode_id LIKE ? OR assets.asset_id LIKE ? OR assets.rfid_id LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

    category_id = args.get("category_id")
    if category_id:
        where_clauses.append("assets.category_id = ?")
        params.append(category_id)

    status_filter = args.get("status")
    if status_filter:
        where_clauses.append("assets.status = ?")
        params.append(status_filter)

    condition_filter = args.get("condition")
    if condition_filter:
        where_clauses.append("assets.condition = ?")
        params.append(condition_filter)

    # Guard on show_archived server-side too, not just hidden client-side
    # inputs -- otherwise a leftover date value could silently empty the
    # active-items table if the toggle got flipped off without clearing it.
    deleted_from = args.get("deleted_from")
    if show_archived and deleted_from:
        error = validate_date_string(deleted_from, "deleted_from")
        if error:
            return None, error
        where_clauses.append("date(assets.deleted_at) >= ?")
        params.append(deleted_from)

    deleted_to = args.get("deleted_to")
    if show_archived and deleted_to:
        error = validate_date_string(deleted_to, "deleted_to")
        if error:
            return None, error
        where_clauses.append("date(assets.deleted_at) <= ?")
        params.append(deleted_to)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    sort_by = args.get("sort_by", "created_at")
    if sort_by not in SORTABLE_COLUMNS:
        return None, (
            jsonify(
                {"error": f"sort_by must be one of: {', '.join(SORTABLE_COLUMNS)}"}
            ),
            400,
        )
    sort_dir = args.get("sort_dir", "desc").lower()
    if sort_dir not in ("asc", "desc"):
        return None, (jsonify({"error": "sort_dir must be 'asc' or 'desc'"}), 400)
    sort_column = SORTABLE_COLUMNS[sort_by]

    conn = get_db_connection()
    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        {where_sql}
        """,
        params,
    ).fetchone()["cnt"]
    assets = conn.execute(
        f"""
        SELECT assets.*, categories.name AS category, staff.username AS deleted_by_username
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        LEFT JOIN staff ON assets.deleted_by = staff.id
        {where_sql}
        ORDER BY {sort_column} {sort_dir.upper()}, assets.id {sort_dir.upper()}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    conn.close()

    return {
        "assets": assets,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_archived": show_archived,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }, None


@assets_bp.route("/assets")
@login_required
def view_assets():
    context, error = get_assets_table_context(request.args)
    if error:
        return error
    return render_template("assets/_table.html", **context)


@assets_bp.route("/assets/stat-cards")
@login_required
def asset_stat_cards():
    """Just the 4 stat-card numbers as HTML, refetched on the 'assets-refreshed'
    HX-Trigger event so cards stay in sync after delete/restore/import (those
    flows don't do a full page reload like add/edit's HX-Redirect)."""
    conn = get_db_connection()
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS all_count,
            SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) AS available_count,
            SUM(CASE WHEN status = 'borrowed' THEN 1 ELSE 0 END) AS borrowed_count,
            SUM(CASE WHEN status = 'maintenance' THEN 1 ELSE 0 END) AS maintenance_count
        FROM assets
        WHERE deleted_at IS NULL
        """
    ).fetchone()
    conn.close()
    return render_template(
        "assets/_stat_cards.html",
        stat_all=counts["all_count"],
        stat_available=counts["available_count"] or 0,
        stat_borrowed=counts["borrowed_count"] or 0,
        stat_maintenance=counts["maintenance_count"] or 0,
    )

    # # # CATEGORIES (lookup table) -------------------------------------------


@assets_bp.route("/categories")
@login_required
def view_categories():
    conn = get_db_connection()
    categories = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    conn.close()
    return jsonify([dict(row) for row in categories])


@assets_bp.route("/categories/manage-form")
@roles_required("admin", "in_charge")
def manage_categories_form():
    conn = get_db_connection()
    # asset_count only counts non-archived assets, so a category whose
    # assets are all archived still shows as deletable.
    categories = conn.execute(
        """
        SELECT categories.*, COUNT(assets.id) AS asset_count
        FROM categories
        LEFT JOIN assets ON assets.category_id = categories.id AND assets.deleted_at IS NULL
        GROUP BY categories.id
        ORDER BY categories.sort_order, categories.name
        """
    ).fetchall()
    conn.close()
    return render_template("assets/_categories_modal.html", categories=categories)


@assets_bp.route("/categories/options")
@login_required
def category_options():
    """<option> tags for the category filter dropdown, in custom order.
    Refetched on the modal's `categories-changed` event; mirrors department_options()."""
    conn = get_db_connection()
    categories = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    conn.close()
    return render_template("assets/_category_options.html", categories=categories)


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
        # New categories go at the end of the order; COALESCE covers the
        # first-ever category where MAX(sort_order) is NULL.
        next_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM categories"
        ).fetchone()["next_order"]
        cursor = conn.execute(
            "INSERT INTO categories (name, sort_order) VALUES (?, ?)", (name, next_order)
        )
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
        target_name=name,
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
        target_name=name,
        details=f"Renamed category '{category['name']}' to '{name}'",
    )

    return jsonify({"message": "Category renamed successfully"})


@assets_bp.route("/categories/reorder", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def reorder_categories():
    """Persists the category order set in the Manage Categories modal.
    Expects {"order": [id, id, ...]} -- every category id, listed in the
    new top-to-bottom order. Mirrors reorder_departments exactly."""
    data, error = get_json_or_error()
    if error:
        return error

    order = data.get("order")
    if not isinstance(order, list) or not order:
        return jsonify({"error": "order must be a non-empty list of category ids"}), 400

    try:
        ordered_ids = [int(cat_id) for cat_id in order]
    except (TypeError, ValueError):
        return jsonify({"error": "order must contain only category ids"}), 400

    if len(set(ordered_ids)) != len(ordered_ids):
        return jsonify({"error": "order contains duplicate category ids"}), 400

    conn = get_db_connection()
    existing_ids = {
        row["id"] for row in conn.execute("SELECT id FROM categories").fetchall()
    }

    if set(ordered_ids) != existing_ids:
        conn.close()
        return (
            jsonify({"error": "order must include exactly the current set of categories"}),
            400,
        )

    try:
        for position, cat_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE categories SET sort_order = ? WHERE id = ?", (position, cat_id)
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        conn.close()
        logger.exception("Failed to save category order")
        return jsonify({"error": "Failed to save category order"}), 500
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="reordered_categories",
        target_table="categories",
        target_id=None,
        details=f"Reordered categories: {', '.join(str(i) for i in ordered_ids)}",
    )

    return jsonify({"message": "Category order saved successfully"})


@assets_bp.route("/categories/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def delete_category(id):
    """Refuses to delete a category with non-archived assets still assigned
    to it -- asks for reassignment first rather than nulling category_id
    or letting a foreign-key error surface as a 500. Mirrors delete_department."""
    conn = get_db_connection()
    category = conn.execute("SELECT * FROM categories WHERE id = ?", (id,)).fetchone()
    if category is None:
        conn.close()
        return jsonify({"error": "Category not found"}), 404

    asset_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM assets WHERE category_id = ? AND deleted_at IS NULL",
        (id,),
    ).fetchone()["cnt"]
    if asset_count > 0:
        conn.close()
        noun = "asset" if asset_count == 1 else "assets"
        return (
            jsonify(
                {
                    "error": f"Can't delete \"{category['name']}\" -- {asset_count} {noun} still assigned to it. Reassign them to another category first."
                }
            ),
            400,
        )

    try:
        conn.execute("DELETE FROM categories WHERE id = ?", (id,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return (
            jsonify({"error": "This category can't be deleted because assets are still assigned to it"}),
            400,
        )
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="deleted_category",
        target_table="categories",
        target_id=id,
        target_name=category["name"],
        details=f"Deleted category: {category['name']}",
    )

    return jsonify({"message": "Category deleted successfully"})


@assets_bp.route("/assets/add-form")
@roles_required("admin", "in_charge")
def add_asset_form():
    conn = get_db_connection()
    categories = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
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

    # barcode_id isn't taken from the form -- it's auto-generated below to
    # match the asset's own asset_id (e.g. "AST00042" for both), so it's
    # always uniform instead of depending on someone typing it right.
    required_fields = ["name"]
    for field in required_fields:
        if field not in data or not data[field]:
            error_msg = f"Missing required field: {field}"
            if request.is_json:
                return jsonify({"error": error_msg}), 200
            conn = get_db_connection()
            categories = conn.execute(
                "SELECT * FROM categories ORDER BY sort_order, name"
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
                "SELECT * FROM categories ORDER BY sort_order, name"
            ).fetchall()
            conn.close()
            return (
                render_template(
                    "assets/_add_modal.html", categories=categories, error=img_error
                ),
                200,
            )

    # Same as above: validate additional images up front so we never leave
    # a half-created asset behind because a later one turned out invalid.
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
                    "SELECT * FROM categories ORDER BY sort_order, name"
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
                "",
                data.get("rfid_id") or None,
                data.get("category_id") or None,
                data.get("serial_code") or None,
                data.get("acquired_date") or None,
                data.get("last_calibrated") or None,
                data.get("notes") or None,
            ),
        )
        new_asset_id = cursor.lastrowid
        generated_code = f"AST{new_asset_id:05d}"
        conn.execute(
            "UPDATE assets SET asset_id = ?, barcode_id = ? WHERE id = ?",
            (generated_code, generated_code, new_asset_id),
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
                    "SELECT * FROM categories ORDER BY sort_order, name"
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
                    "SELECT * FROM categories ORDER BY sort_order, name"
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
        logger.error(f"IntegrityError: {e}")
        conn.rollback()
        if saved_image_path:
            delete_asset_image(saved_image_path)
        for p in saved_additional_paths.values():
            delete_asset_image(p)
        error_msg = "A database constraint was violated. Check for duplicate barcode/serial/ID values."
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        conn2 = get_db_connection()
        categories = conn2.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
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
        target_name=f"{generated_code} — {data['name']}",
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


REQUIRED_CSV_COLUMNS = ("name",)


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
    """JSON for API callers, modal re-render with inline error otherwise.
    HX-Retarget/HX-Reswap force the swap onto #modal-container -- needed
    because the preview-step submit targets the inner #import-step-content
    div, and without the override this re-render would nest inside it
    instead of replacing the whole modal."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    response = make_response(
        render_template("assets/_import_modal.html", error=error_msg), 200
    )
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


def _parse_import_deleted_at(raw):
    """A row is archived if its deleted_at cell is non-blank, active otherwise
    -- no separate "import as archived" toggle needed. Accepts a bare date
    or a full timestamp (what a round-tripped archived-view export contains)
    and normalizes to the same format delete_asset writes. Returns None if
    unparseable, so the caller treats it as a row error instead of silently
    defaulting to "active" and un-archiving something the CSV meant to keep archived."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


@assets_bp.route("/assets/import", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("5 per minute")
def import_assets_csv():
    if "file" not in request.files:
        return _import_error_response("Missing file upload (field name: 'file')", 400)

    file = request.files["file"]
    if not file.filename:
        return _import_error_response("No file selected", 400)

    mode = request.form.get("mode", "commit")
    if mode not in ("preview", "commit"):
        return _import_error_response("mode must be 'preview' or 'commit'", 400)
    dry_run = mode == "preview"

    try:
        content = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return _import_error_response("File must be UTF-8 encoded CSV", 400)

    reader = csv.DictReader(io.StringIO(content))
    raw_fieldnames = reader.fieldnames or []

    # Accept both canonical snake_case headers and Export's human-readable labels
    # (Name, Barcode ID, ...), so re-importing our own export just works.
    reverse_labels = {label.lower(): key for key, label in EXPORT_FIELD_LABELS.items()}
    header_map = {}
    for h in raw_fieldnames:
        normalized = (h or "").strip()
        header_map[h] = reverse_labels.get(
            normalized.lower(), normalized.lower().replace(" ", "_")
        )
    canonical_fieldnames = list(header_map.values())

    missing_cols = [c for c in REQUIRED_CSV_COLUMNS if c not in canonical_fieldnames]
    if missing_cols:
        return _import_error_response(
            f"CSV missing required column(s): {', '.join(missing_cols)}", 400
        )

    conn = get_db_connection()
    # sqlite3's default isolation mode auto-commits before statements it doesn't recognize,
    # including our raw SAVEPOINT/ROLLBACK TO below -- would silently commit each row before
    # preview mode's final rollback. isolation_level=None hands control to our own BEGIN/COMMIT.
    conn.isolation_level = None
    conn.execute("BEGIN")
    created, errors, new_categories = [], [], []
    seen_category_names = set()

    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {header_map[h]: v for h, v in raw_row.items() if h in header_map}
        name = (row.get("name") or "").strip()
        # barcode_id isn't read from the CSV -- it's always auto-generated
        # to match asset_id, same as single-asset Add. Any barcode_id column
        # in the file is ignored.
        if not name:
            errors.append({"row": row_num, "error": "name is required"})
            continue

        date_error = validate_date_fields(row, ("acquired_date", "last_calibrated"))
        if date_error:
            errors.append({"row": row_num, "error": "invalid date format"})
            continue

        # Blank deleted_at -> active, non-blank -> archived. See
        # _parse_import_deleted_at for why unparseable is a hard error.
        deleted_at_raw = (row.get("deleted_at") or "").strip()
        deleted_at = None
        if deleted_at_raw:
            deleted_at = _parse_import_deleted_at(deleted_at_raw)
            if deleted_at is None:
                errors.append(
                    {
                        "row": row_num,
                        "error": "invalid deleted_at format (expected YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
                    }
                )
                continue

        conn.execute("SAVEPOINT row_import")
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
                    if category_name not in seen_category_names:
                        seen_category_names.add(category_name)
                        new_categories.append(category_name)
                else:
                    category_id = cat_row["id"]

            cursor = conn.execute(
                """
                INSERT INTO assets (asset_id, name, barcode_id, rfid_id, category_id, serial_code, acquired_date, last_calibrated, notes, deleted_at, deleted_by)
                VALUES ('', ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    row.get("rfid_id") or None,
                    category_id,
                    row.get("serial_code") or None,
                    row.get("acquired_date") or None,
                    row.get("last_calibrated") or None,
                    row.get("notes") or None,
                    deleted_at,
                    g.staff["id"] if deleted_at is not None else None,
                ),
            )
            new_id = cursor.lastrowid
            generated_code = f"AST{new_id:05d}"
            conn.execute(
                "UPDATE assets SET asset_id = ?, barcode_id = ? WHERE id = ?",
                (generated_code, generated_code, new_id),
            )
            conn.execute("RELEASE row_import")
            created.append(
                {
                    "row": row_num,
                    "id": new_id,
                    "barcode_id": generated_code,
                    "archived": deleted_at is not None,
                }
            )
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK TO row_import")
            conn.execute("RELEASE row_import")
            errors.append(
                {"row": row_num, "error": f"duplicate or constraint violation: {e}"}
            )

    # Preview mode runs every row for real (category creation, UNIQUE
    # checks and all) so it can't disagree with what an actual commit would
    # do -- it just discards the whole batch at the end instead of keeping it.
    if dry_run:
        conn.execute("ROLLBACK")
    else:
        conn.execute("COMMIT")
    conn.close()

    archived_count = sum(1 for c in created if c["archived"])

    if dry_run:
        # No log_action here -- nothing was actually persisted, rolled back above.
        return render_template(
            "assets/_import_preview.html",
            rows_to_create=len(created),
            rows_to_archive=archived_count,
            errors=errors,
            new_categories=new_categories,
        )

    detail_suffix = f" ({archived_count} archived)" if archived_count else ""
    log_action(
        staff_id=g.staff["id"],
        action="bulk_imported_assets",
        target_table="assets",
        details=f"Imported {len(created)} asset(s) via CSV{detail_suffix}, {len(errors)} error(s)",
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
            created_ids=[c["id"] for c in created],
            errors=errors,
            archived_count=archived_count,
        )
    )
    if created:
        response.headers["HX-Trigger"] = "assets-refreshed"
    return response


# Matches the import columns above (plus read-only status/condition, and
# deleted_at for the archived view) so export -> edit -> import round-trips.
EXPORT_CSV_COLUMNS = (
    "asset_id",
    "name",
    "barcode_id",
    "rfid_id",
    "category",
    "status",
    "condition",
    "serial_code",
    "acquired_date",
    "last_calibrated",
    "notes",
)

# Labels for the export modal's field checklist; keys mirror EXPORT_CSV_COLUMNS
# (+ deleted_at, archived-only) so checklist and actual columns can't drift.
EXPORT_FIELD_LABELS = {
    "asset_id": "Asset ID",
    "name": "Name",
    "barcode_id": "Barcode ID",
    "rfid_id": "RFID Tag",
    "category": "Category",
    "status": "Status",
    "condition": "Condition",
    "serial_code": "Serial Code",
    "acquired_date": "Acquired Date",
    "last_calibrated": "Last Calibrated",
    "notes": "Notes",
    "deleted_at": "Deleted At",
}


def _default_export_basename(show_archived):
    return (
        f"assets_{'archived' if show_archived else 'active'}_"
        f"{datetime.now().strftime('%Y%m%d')}"
    )


def _default_export_dir():
    """<app root>/export -- fine to use the app folder directly since this
    is a self-hosted internal tool, not a shared public host."""
    return os.path.join(current_app.root_path, "export")


# Strips anything that isn't a safe filename char, and strips any typed
# extension too -- the export format decides the real extension, not the user.
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]+")
_FILENAME_EXT_RE = re.compile(r"\.(csv|xlsx?)$", re.IGNORECASE)


def _sanitize_export_filename(raw, fallback):
    raw = (raw or "").strip()
    if not raw:
        return fallback
    raw = _FILENAME_EXT_RE.sub("", raw).strip()
    raw = _FILENAME_SAFE_RE.sub("_", raw).strip("_")
    return raw or fallback


def _resolve_within_root(raw_path, default_dir):
    """Resolves raw_path to an absolute path guaranteed to stay inside the
    app root -- anything that would escape it (e.g. "../../etc") falls back
    to default_dir instead. Returns (resolved_dir, was_clamped). Shared by
    the export save-path resolution and the folder browser so both enforce
    the same boundary."""
    app_root = os.path.realpath(current_app.root_path)

    raw_path = (raw_path or "").strip()
    if not raw_path:
        return default_dir, False

    candidate = raw_path if os.path.isabs(raw_path) else os.path.join(app_root, raw_path)
    resolved = os.path.realpath(candidate)

    try:
        inside_root = os.path.commonpath([resolved, app_root]) == app_root
    except ValueError:
        # e.g. different drives on Windows -- definitely not inside app_root
        inside_root = False

    if not inside_root:
        return default_dir, True
    return resolved, False


def _resolve_export_dir(raw_path):
    """Resolves the user-entered save folder for an export. Falls back
    silently rather than erroring, since there's no good way to surface a
    validation error mid-download."""
    return _resolve_within_root(raw_path, _default_export_dir())


_FOLDER_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


@assets_bp.route("/assets/export/browse")
@login_required
def browse_export_folders():
    """Lists subfolders under the app root for the export modal's folder
    picker. Read-only and clamped to the app root same as the export
    save-path, so login_required is enough -- it doesn't expose anything
    a logged-in user couldn't already reach."""
    app_root = os.path.realpath(current_app.root_path)
    resolved, _ = _resolve_within_root(request.args.get("path", ""), app_root)

    if not os.path.isdir(resolved):
        resolved = app_root

    try:
        folders = sorted(
            entry
            for entry in os.listdir(resolved)
            if os.path.isdir(os.path.join(resolved, entry)) and not entry.startswith(".")
        )
    except OSError as e:
        return jsonify({"error": f"Could not list folder: {e}"}), 400

    def _rel(p):
        rel = os.path.relpath(p, app_root)
        return "" if rel == "." else rel.replace(os.sep, "/")

    parent_path = None
    if resolved != app_root:
        parent_path = _rel(os.path.dirname(resolved))

    return jsonify({"path": _rel(resolved), "parent_path": parent_path, "folders": folders})


@assets_bp.route("/assets/export/browse/mkdir", methods=["POST"])
@login_required
def create_export_folder():
    """Creates a subfolder from the folder picker, e.g. "export/2026", so
    staff don't need server access to make one first. Folder name is
    restricted to a safe charset (rejected, not sanitized) since unlike the
    export filename there's no sensible fallback to substitute."""
    app_root = os.path.realpath(current_app.root_path)
    resolved, _ = _resolve_within_root(request.form.get("path", ""), app_root)
    if not os.path.isdir(resolved):
        resolved = app_root

    name = (request.form.get("name") or "").strip()
    if not name or not _FOLDER_NAME_RE.match(name):
        return jsonify(
            {"error": "Folder name can only contain letters, numbers, hyphens, and underscores"}
        ), 400

    new_dir = os.path.join(resolved, name)
    try:
        os.makedirs(new_dir, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Could not create folder: {e}"}), 400

    return jsonify({"path": os.path.relpath(new_dir, app_root).replace(os.sep, "/")})


@assets_bp.route("/assets/export-form")
@login_required
def export_assets_form():
    """Opens the export modal -- read-only like the export GET route, so any
    logged-in staff can see it. show_archived matches whichever table view
    the user was looking at, so filename/field list/description line up."""
    show_archived = request.args.get("show_archived") == "1"
    fields = list(EXPORT_CSV_COLUMNS)
    if show_archived:
        fields.append("deleted_at")
    return render_template(
        "assets/_export_modal.html",
        show_archived=show_archived,
        default_filename=_default_export_basename(show_archived),
        default_export_dir=_default_export_dir(),
        fields=[(col, EXPORT_FIELD_LABELS[col]) for col in fields],
    )


@assets_bp.route("/assets/export", methods=["GET", "POST"])
@login_required
def export_assets():
    """GET is read-only and login_required-only (not roles_required) since
    exporting doesn't mutate anything; respects show_archived so it mirrors
    whatever table view is on screen. Supports CSV (round-trips through
    Import CSV) and Excel, a chosen column subset, and a chosen filename.

    POST additionally saves a copy to a server folder -- gated to POST to
    keep the plain-link GET side-effect free and to get CSRF protection on
    the disk write. request.values covers both GET args and POST form so
    the rest of the function doesn't need to branch on method."""
    show_archived = request.values.get("show_archived") == "1"
    export_format = request.values.get("format", "csv").lower()
    if export_format not in ("csv", "xlsx"):
        export_format = "csv"

    available_columns = list(EXPORT_CSV_COLUMNS)
    if show_archived:
        available_columns.append("deleted_at")

    selected = request.values.getlist("fields")
    if selected:
        columns = [c for c in available_columns if c in selected]
        if not columns:  # everything requested was invalid/unknown -- don't export a blank file
            columns = available_columns
    else:
        columns = available_columns

    conn = get_db_connection()
    where_clause = (
        "assets.deleted_at IS NOT NULL" if show_archived else "assets.deleted_at IS NULL"
    )
    assets = conn.execute(
        f"""
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE {where_clause}
        ORDER BY assets.asset_id
        """
    ).fetchall()
    conn.close()

    basename = _sanitize_export_filename(
        request.values.get("filename"), _default_export_basename(show_archived)
    )

    if export_format == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "Archived Assets" if show_archived else "Assets"

        header_font = Font(bold=True)
        ws.append([EXPORT_FIELD_LABELS.get(col, col) for col in columns])
        for cell in ws[1]:
            cell.font = header_font

        for asset in assets:
            ws.append([asset[col] if asset[col] is not None else "" for col in columns])

        # Autosize columns roughly to their content, since Excel opens
        # every column at a fixed narrow default width otherwise.
        for i, col in enumerate(columns, start=1):
            longest = max(
                [len(col)] + [len(str(asset[col] or "")) for asset in assets]
            )
            ws.column_dimensions[get_column_letter(i)].width = min(
                max(longest + 2, 10), 40
            )
        ws.freeze_panes = "A2"

        buf = io.BytesIO()
        wb.save(buf)
        file_bytes = buf.getvalue()
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        export_format = "csv"
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([EXPORT_FIELD_LABELS.get(col, col) for col in columns])
        for asset in assets:
            writer.writerow([asset[col] if asset[col] is not None else "" for col in columns])
        # BOM so Excel opens accented characters correctly on double-click.
        file_bytes = output.getvalue().encode("utf-8-sig")
        content_type = "text/csv"

    filename = f"{basename}.{export_format}"

    response = make_response(file_bytes)
    response.headers["Content-Type"] = content_type
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"

    if request.method == "POST":
        save_dir, was_clamped = _resolve_export_dir(request.values.get("save_path"))
        try:
            os.makedirs(save_dir, exist_ok=True)
            saved_path = os.path.join(save_dir, filename)
            with open(saved_path, "wb") as f:
                f.write(file_bytes)
            response.headers["X-Export-Saved-Path"] = saved_path
            if was_clamped:
                response.headers["X-Export-Warning"] = (
                    "The folder entered was outside the app directory, "
                    "so the export was saved to the default export folder instead."
                )
        except OSError as e:
            logger.warning(f"Could not save export copy to disk: {e}")
            response.headers["X-Export-Warning"] = (
                "Could not save a copy on the server; your download still completed."
            )

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
    categories = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    additional_images = _fetch_asset_images(id, conn)
    conn.close()
    # Where Cancel sends the user back to (Details modal vs just closing).
    # Defaults to 'table' so a bad/missing value fails toward "just close".
    return_to = request.args.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"
    return render_template(
        "assets/_edit_modal.html",
        asset=asset,
        categories=categories,
        additional_images=additional_images,
        error=None,
        return_to=return_to,
    )


def _edit_error_response(
    payload_or_msg, status, asset=None, categories=None, asset_id=None, return_to="table"
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
        categories = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    additional_images = _fetch_asset_images(asset["id"], conn)
    conn.close()
    error_msg = (
        payload_or_msg.get("error")
        if isinstance(payload_or_msg, dict)
        else payload_or_msg
    )
    if return_to not in ("table", "details"):
        return_to = "table"
    return (
        render_template(
            "assets/_edit_modal.html",
            asset=asset,
            categories=categories,
            additional_images=additional_images,
            error=error_msg,
            return_to=return_to,
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

    # From the hidden field the edit form sets on load, so Cancel still
    # routes correctly if this submit fails validation and re-renders.
    return_to = data.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"

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
            return_to=return_to,
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
            return_to=return_to,
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
                {"error": img_error}, 400, asset=asset, asset_id=id, return_to=return_to
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
                    {"error": img_error}, 400, asset=asset, asset_id=id, return_to=return_to
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

    # "Set as Main": swap main image with an additional-image slot, unless either side
    # is also being replaced/removed this submit -- an explicit replace/remove wins.
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
                    {"error": img_error}, 400, asset=asset, asset_id=id, return_to=return_to
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
                        {"error": img_error}, 400, asset=asset, asset_id=id, return_to=return_to
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
        return _edit_error_response(
            {
                "error": "A database constraint was violated. Check for duplicate barcode/RFID/serial values."
            },
            400,
            asset=asset,
            asset_id=id,
            return_to=return_to,
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
        # data.get(..., asset["name"]) mirrors the UPDATE's own fallback,
        # so this reflects the actually-saved name.
        target_name=f"{asset['asset_id']} — {data.get('name', asset['name'])}",
        details=f"Updated asset {asset['asset_id']}",
    )

    if new_condition == "disposed":
        log_action(
            staff_id=g.staff["id"],
            action="disposed_asset",
            target_table="assets",
            target_id=asset["id"],
            target_name=f"{asset['asset_id']} — {data.get('name', asset['name'])}",
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
    return_to = request.args.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"
    return render_template(
        "assets/_delete_modal.html", asset=asset, error=None, return_to=return_to
    )


@assets_bp.route("/assets/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def delete_asset(id):
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    return_to = data.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"

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
            render_template(
                "assets/_delete_modal.html",
                asset=asset,
                error=error_msg,
                return_to=return_to,
            ),
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
            render_template(
                "assets/_delete_modal.html",
                asset=asset,
                error=error_msg,
                return_to=return_to,
            ),
            200,
        )

    deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "UPDATE assets SET deleted_at = ?, deleted_by = ? WHERE id = ?",
            (deleted_at, g.staff["id"], id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        error_msg = "A database constraint was violated. Check for duplicate barcode/username/ID values."
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "assets/_delete_modal.html",
                asset=asset,
                error=error_msg,
                return_to=return_to,
            ),
            200,
        )
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="deleted_asset",
        target_table="assets",
        target_id=id,
        target_name=f"{asset['asset_id']} — {asset['name']}",
        details=f"Soft-deleted asset {asset['asset_id']}",
    )

    if request.is_json:
        return jsonify({"message": "Asset soft deleted successfully"})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
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
    """Powers the Undo button on the delete toast. Kept separate from
    restore_asset (same DB change) for two reasons: it's admin/in_charge
    like delete, not admin-only like restore, since undoing your own
    just-made delete shouldn't need extra permissions; and it never prompts
    for a condition, since delete doesn't touch condition so there's nothing
    to re-assess."""
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
            "UPDATE assets SET deleted_at = NULL, deleted_by = NULL, status = 'available' WHERE id = ?",
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
        target_name=f"{asset['asset_id']} — {asset['name']}",
        details=f"Undid delete of asset {asset['asset_id']}",
    )

    return jsonify({"message": "Delete undone"})


@assets_bp.route("/assets/bulk-delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("10 per minute")
def bulk_delete_assets():
    """Soft-deletes multiple assets. Unlike delete_asset, one bad row
    (not found/already deleted/checked out) goes into `skipped` instead of
    failing the whole batch -- expected outcome for a multi-select UI."""
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
                "UPDATE assets SET deleted_at = ?, deleted_by = ? WHERE id = ?",
                (deleted_at, g.staff["id"], asset_id),
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
    """Restores multiple archived assets. Mirrors bulk_delete_assets's
    shape but admin-only (matching single-item restore_asset, since
    reversing a disposal is a bigger call than deleting). Disposed assets
    are always skipped here rather than restored, since picking a new
    condition needs the single-item restore modal's dropdown -- no per-row
    UI for that in a bulk action."""
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
                "UPDATE assets SET deleted_at = NULL, deleted_by = NULL, status = 'available' WHERE id = ?",
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
    return_to = request.args.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"
    return render_template(
        "assets/_restore_modal.html", asset=asset, error=None, return_to=return_to
    )


def _restore_error_response(error_msg, status, asset_id, return_to="table"):
    """Mirrors _edit_error_response: JSON for API callers, a re-rendered
    modal with the error inline for htmx/form callers."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    conn.close()
    if return_to not in ("table", "details"):
        return_to = "table"
    return (
        render_template(
            "assets/_restore_modal.html", asset=asset, error=error_msg, return_to=return_to
        ),
        200,
    )


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
    return_to = data.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"

    valid_conditions = ("functional", "inoperable", "for_repair", "disposed")
    if condition is not None and condition not in valid_conditions:
        return _restore_error_response(
            f"condition must be one of: {', '.join(valid_conditions)}",
            400,
            id,
            return_to=return_to,
        )
    if condition == "disposed":
        return _restore_error_response(
            "Cannot restore an asset with condition 'disposed' -- choose a different condition",
            400,
            id,
            return_to=return_to,
        )

    conn = get_db_connection()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    if asset["deleted_at"] is None:
        conn.close()
        return _restore_error_response("Asset is not deleted", 400, id, return_to=return_to)

    if asset["condition"] == "disposed" and condition is None:
        conn.close()
        return _restore_error_response(
            "This asset's condition is 'disposed' -- choose a new condition to restore it",
            400,
            id,
            return_to=return_to,
        )

    new_condition = condition if condition is not None else asset["condition"]

    try:
        conn.execute(
            "UPDATE assets SET deleted_at = NULL, deleted_by = NULL, condition = ?, status = 'available' WHERE id = ?",
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
        target_name=f"{asset['asset_id']} — {asset['name']}",
        details=f"Restored asset {asset['asset_id']} (condition set to '{new_condition}')",
    )

    if request.is_json:
        return jsonify({"message": "Asset restored successfully"})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "assets-refreshed": True,
            "show-toast": {
                "message": f"{asset['asset_id']} restored successfully",
                "type": "success",
                "action": {
                    "label": "Undo",
                    # Re-archiving undoes a restore; works permission-wise too
                    # since delete (admin/in_charge) is looser than restore (admin).
                    "url": f"/api/v1/assets/{id}/delete",
                    "method": "POST",
                    "successMessage": "Restore undone",
                },
            },
        }
    )
    return response


@assets_bp.route("/assets/barcode/<barcode>")
@login_required
@limiter.limit("40 per minute")
def get_asset_by_barcode(barcode):
    conn = get_db_connection()
    asset = conn.execute(
        """
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE (UPPER(assets.barcode_id) = UPPER(?) OR UPPER(assets.rfid_id) = UPPER(?)) AND assets.deleted_at IS NULL
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


@assets_bp.route("/assets/by-barcode/<string:barcode>")
@login_required
def asset_by_barcode(barcode):
    """Exact-match barcode lookup for the search box's Enter-key shortcut,
    as opposed to its live substring filter which only narrows the table.
    Matches archived items too, since scanning an archived barcode should
    still show its details. Renders the same details modal on a hit, or a
    shared "no match" modal on a miss -- both return 200 (not 404) since
    htmx.ajax() only swaps on 2xx and "not found" is a valid result here."""
    conn = get_db_connection()
    asset = conn.execute(
        "SELECT id FROM assets WHERE UPPER(barcode_id) = UPPER(?)", (barcode,)
    ).fetchone()
    conn.close()
    if asset is None:
        return render_template(
            "_barcode_not_found_modal.html", kind="inventory item", barcode=barcode
        )
    return asset_details_modal(asset["id"])


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
