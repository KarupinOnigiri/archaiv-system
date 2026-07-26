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
    """Builds everything _table.html needs (assets, total_count, limit,
    offset, show_archived, sort_by, sort_dir) from a request.args-like
    object. Shared by view_assets() (the htmx partial endpoint) and
    assets_page() in routes/pages.py (the full page load) so the two
    can't quietly drift out of sync on what the template requires --
    which is exactly what happened before this function existed:
    _table.html needed 'offset' and pages.py never supplied it.

    Returns (context_dict, None) on success, or (None, error_response)
    on a bad query param -- callers should return the error response
    as-is for an API/htmx caller, or fall back to defaults for a full
    page load where a stray bad query string shouldn't 500/400 the
    whole page.
    """
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
            "(assets.name LIKE ? OR assets.barcode_id LIKE ? OR assets.asset_id LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like])

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

    # Only meaningful when viewing archived items -- deleted_at is NULL for
    # everything else, and guarding on show_archived here (rather than just
    # relying on the date inputs being hidden client-side) means a leftover
    # date value can't ever silently empty out the active-items table if
    # the toggle gets flipped off without the fields being cleared.
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
    """Returns just the 4 stat-card numbers as HTML. Listened to via the
    'assets-refreshed' HX-Trigger event (see #stat-cards-container in
    assets/index.html) so the cards stay in sync with the table after
    delete/restore/import -- those flows don't do a full page reload the
    way add/edit's HX-Redirect does, so without this the cards go stale."""
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
    the modal re-rendered with an inline error for htmx/form callers.

    HX-Retarget/HX-Reswap force the swap back onto #modal-container
    regardless of what the triggering request declared as its target.
    That matters here specifically because the initial (preview-step)
    submit targets the inner #import-step-content div, not the modal
    itself -- without this override, this full modal re-render would
    get nested inside that inner div instead of replacing the modal."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    response = make_response(
        render_template("assets/_import_modal.html", error=error_msg), 200
    )
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


def _parse_import_deleted_at(raw):
    """Best-practice rule for the archive/active split on import: a row is
    archived if (and only if) its deleted_at cell is non-blank, active
    otherwise -- so importing is a data-driven mirror of the delete/restore
    actions rather than needing a separate "import as archived" toggle.

    Accepts either a bare date (YYYY-MM-DD, easy to type/hand-edit) or a
    full timestamp (YYYY-MM-DD HH:MM:SS, what a round-tripped export of the
    archived view actually contains -- see delete_asset, which stores
    deleted_at in that exact format). Normalizes to the same
    "%Y-%m-%d %H:%M:%S" format used everywhere else deleted_at is written,
    so an archived row imported this way is indistinguishable from one
    archived through the UI. Returns None if raw is unparseable in either
    format -- the caller treats that as a row-level error rather than
    silently falling back to "active", since guessing wrong here would
    quietly un-archive something the CSV clearly intended to keep archived."""
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

    # Import used to only accept the canonical snake_case keys (name,
    # barcode_id, ...), but Export writes human-readable labels (Name,
    # Barcode ID, ...) via EXPORT_FIELD_LABELS -- so re-importing the app's
    # own export, or the archived view's export (which is exactly what the
    # import modal's help text describes as a supported workflow), failed
    # immediately on "missing required column(s)". This maps either form
    # back to the canonical key so both work; an unrecognized header is
    # just lowercased/underscored and, if it doesn't match anything the
    # loop below reads, silently ignored -- same as an unknown column
    # always was.
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
    # Manual transaction control for this connection only (doesn't affect
    # any other request/route): Python's sqlite3 wrapper, in its default
    # mode, silently COMMITs any open transaction before executing a
    # statement it doesn't recognize as INSERT/UPDATE/DELETE/SELECT --
    # which includes the raw SAVEPOINT/RELEASE/ROLLBACK TO statements
    # below. Left in that default mode, every row's SAVEPOINT would
    # implicitly commit the *previous* row to disk before it even started,
    # so preview mode's rollback-the-whole-batch-at-the-end had nothing
    # left to roll back -- rows were getting permanently created except
    # the ones that errored. isolation_level=None hands full control to
    # the explicit BEGIN/COMMIT/ROLLBACK calls below instead.
    conn.isolation_level = None
    conn.execute("BEGIN")
    created, errors, new_categories = [], [], []
    seen_category_names = set()

    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {header_map[h]: v for h, v in raw_row.items() if h in header_map}
        name = (row.get("name") or "").strip()
        barcode_id = (row.get("barcode_id") or "").strip()
        if not name or not barcode_id:
            errors.append({"row": row_num, "error": "name and barcode_id are required"})
            continue

        date_error = validate_date_fields(row, ("acquired_date", "last_calibrated"))
        if date_error:
            errors.append({"row": row_num, "error": "invalid date format"})
            continue

        # Blank/missing deleted_at -> active; a non-blank value archives the
        # row on import. See _parse_import_deleted_at for why an unparseable
        # (rather than blank) value is a hard error instead of a silent
        # fallback to active.
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
                VALUES ('', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    deleted_at,
                    g.staff["id"] if deleted_at is not None else None,
                ),
            )
            new_id = cursor.lastrowid
            conn.execute(
                "UPDATE assets SET asset_id = ? WHERE id = ?",
                (f"AST-{new_id:05d}", new_id),
            )
            conn.execute("RELEASE row_import")
            created.append(
                {
                    "row": row_num,
                    "id": new_id,
                    "barcode_id": barcode_id,
                    "archived": deleted_at is not None,
                }
            )
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK TO row_import")
            conn.execute("RELEASE row_import")
            errors.append(
                {"row": row_num, "error": f"duplicate or constraint violation: {e}"}
            )

    # Preview mode runs every row for real -- including auto-created
    # categories and actual UNIQUE-constraint checks against both the DB
    # and the rest of the same file -- so it can't disagree with what a
    # real commit would do (e.g. two rows in the same file sharing a
    # barcode). It just discards the whole batch here instead of keeping
    # it, so nothing from a preview is ever actually persisted.
    if dry_run:
        conn.execute("ROLLBACK")
    else:
        conn.execute("COMMIT")
    conn.close()

    archived_count = sum(1 for c in created if c["archived"])

    if dry_run:
        # Nothing was persisted (rolled back above), so this deliberately
        # doesn't call log_action -- an audit trail entry for an import that
        # never happened would be misleading.
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


# Columns match REQUIRED_CSV_COLUMNS/OPTIONAL_CSV_COLUMNS above (plus
# read-only status/condition, and deleted_at when exporting the archived
# view) so a round trip through export -> edit -> import stays consistent.
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

# Human-readable labels for the export modal's field checklist. Keys mirror
# EXPORT_CSV_COLUMNS (+ deleted_at, archived-only) so the checklist and the
# actual export columns can never drift out of sync with each other.
EXPORT_FIELD_LABELS = {
    "asset_id": "Asset ID",
    "name": "Name",
    "barcode_id": "Barcode ID",
    "rfid_id": "RFID ID",
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
    """<app root>/export -- current_app.root_path is the folder app.py
    lives in, i.e. what the person means by "the root folder of the
    system" here (this is a self-hosted internal tool, not a shared public
    host, so exposing/using that folder is the intended behavior)."""
    return os.path.join(current_app.root_path, "export")


# Only safe filename characters survive; anything else (path separators,
# quotes, whitespace, etc.) becomes an underscore. Extension is stripped
# too, since the export format -- not whatever the user typed -- decides
# it, so ".exe" or a mismatched ".csv" typed while Excel is selected can't
# sneak through into the actual downloaded filename.
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
    """Resolves raw_path to an absolute path that's guaranteed to stay
    inside the app root -- a relative path (e.g. "export" or "export/2026")
    is resolved against the app root, and an absolute path is only honored
    if it's already inside the app root; anything that would escape it
    (e.g. "../../etc", or an unrelated absolute path) falls back to
    default_dir instead. Returns (resolved_dir, was_clamped). Shared by the
    final export save-path resolution and the folder browser below, so
    both enforce exactly the same boundary."""
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
    """Resolves the user-entered save folder for an actual export. Falls
    back silently (no error surfaced) since this runs as a side effect of
    a file download and there's no good way to show a validation error
    mid-download; see _resolve_within_root for the boundary rule itself."""
    return _resolve_within_root(raw_path, _default_export_dir())


_FOLDER_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


@assets_bp.route("/assets/export/browse")
@login_required
def browse_export_folders():
    """Lists subfolders under the app root so the export modal's folder
    picker can let staff click through the server's real directory
    structure instead of typing a path by hand. Read-only (just
    os.listdir) and scoped to the app root the same way the final export
    save-path is clamped, so this can't be used to peek at the rest of the
    server's filesystem -- only login_required, since it doesn't reveal
    anything the export route's own save-path clamping wouldn't already
    let a logged-in user reach anyway."""
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
    """Creates a subfolder from within the folder picker, e.g. so staff can
    make "export/2026" on the fly instead of needing someone with server
    access to create it first. Same app-root boundary as everything else
    here; the folder name itself is restricted to a safe character set
    (not just sanitized) since, unlike the export filename, there's no
    sensible fallback to silently substitute for a bad folder name."""
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
    """Opens the export modal (fields, filename, format, save folder) into
    #modal-container -- read-only like the export route's GET path, so any
    logged-in staff can see it. show_archived is passed through the same
    way the page's other modals get it, so the modal's default filename,
    field list, and description match whichever table view (active/
    archived) the user was looking at."""
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
    """Read-only for GET, so any logged-in staff can use it -- deliberately
    not roles_required like add/import, since exporting doesn't mutate
    anything. Mirrors the current table view: respects show_archived the
    same way the Export Data button's link already passes it, rather than
    always dumping every asset regardless of what's on screen. Supports
    CSV (default, for round-tripping back through Import CSV) and Excel, a
    user-chosen subset of columns, and a user-chosen filename.

    POST (used by the export modal, via fetch so it can also read the
    response headers below) additionally writes a copy of the export to a
    folder on the server -- this is the one side effect this route has, so
    it's gated to POST specifically to keep the plain-link GET path free of
    side effects and to get CSRF protection on the part that touches disk.
    request.values covers both request.args (GET) and request.form (POST)
    so the rest of this function doesn't need to branch on method."""
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
        # utf-8-sig (BOM) so Excel opens accented/special characters correctly
        # when double-clicking the CSV directly, matching how the CSV import
        # side already expects/tolerates a BOM.
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
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    additional_images = _fetch_asset_images(id, conn)
    conn.close()
    # Where to send the user on Cancel: back to the Details modal if that's
    # where Edit was opened from, or just close (back to the table) if it
    # was opened directly from a row action. Defaults to 'table' so an
    # unrecognized/missing value fails toward the safer "just close" option.
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
        categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
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

    # Carried through from the hidden field the edit form sets on load, so
    # Cancel still routes correctly (back to Details, or just closes) if
    # this submit fails validation and the modal has to be re-rendered.
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
