"""Query-building and mutation logic for routes/assets.py (assets + categories).

Same convention as services/member_service.py: every function takes an already-open
`conn` (the route owns open/close, except import_assets_csv which needs raw
isolation-level control and opens its own) and returns `(data, error)`. `error` is
`None` on success, or `(error_payload, status, context)` on failure -- `error_payload`
is a plain dict (for jsonify), `context` holds extra objects (e.g. {"asset": asset})
the route may need to re-render an inline-error template on non-JSON requests.
"""

import csv
import io
import logging
import os
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from database import get_db_connection, sqlite3
from utils.logger import log_action
from utils.uploads import save_asset_image, delete_asset_image, validate_image_upload
from utils.validators import validate_date_fields

logger = logging.getLogger(__name__)

ADDITIONAL_IMAGE_SLOTS = (1, 2)
VALID_CONDITIONS = ("functional", "inoperable", "for_repair", "disposed")
DETAILS_RECENT_ACTIVITY_LIMIT = 3

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

REQUIRED_CSV_COLUMNS = ("name",)

OPTIONAL_CSV_COLUMNS = (
    "rfid_id",
    "category",
    "serial_code",
    "acquired_date",
    "last_calibrated",
    "notes",
)

# Matches the import columns (plus read-only status/condition/deleted_at) for round-tripping.
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

# Labels for the export checklist; keys mirror EXPORT_CSV_COLUMNS so they can't drift apart.
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

_FOLDER_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _no_ctx(error_payload, status):
    return (error_payload, status, {})


def _ensure_category_sort_order_column():
    """Adds sort_order to categories if missing, backfilled alphabetically. Safe to rerun."""
    conn = get_db_connection()
    try:
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(categories)").fetchall()}
        if "sort_order" not in existing_cols:
            conn.execute("ALTER TABLE categories ADD COLUMN sort_order INTEGER")
            rows = conn.execute("SELECT id FROM categories ORDER BY name").fetchall()
            for position, row in enumerate(rows):
                conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (position, row["id"]))
            conn.commit()
            logger.info("Added categories.sort_order and backfilled %d row(s)", len(rows))
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Failed to ensure categories.sort_order column")
    finally:
        conn.close()


# Runs once at import time, same as it did at module load in routes/assets.py.
_ensure_category_sort_order_column()


def fetch_asset_images(asset_id, conn):
    """Returns {1: row_or_None, 2: row_or_None} so templates can index by slot number."""
    rows = conn.execute("SELECT * FROM asset_images WHERE asset_id = ?", (asset_id,)).fetchall()
    by_slot = {slot: None for slot in ADDITIONAL_IMAGE_SLOTS}
    for row in rows:
        by_slot[row["slot"]] = row
    return by_slot


def _validate_category_id(conn, category_id, required=False):
    """Local, non-Flask mirror of utils.validators.validate_lookup_id(conn, "categories", ...)."""
    if category_id is None:
        if required:
            return {"error": "Missing required field: category_id"}, 400
        return None
    row = conn.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
    if row is None:
        return {"error": "category_id does not reference an existing entry"}, 400
    return None


# # # LIST / VIEW ------------------------------------------------------------


def get_assets_table_context(conn, args, limit, offset):
    """Shared table context for view_assets() and assets_page(), so they can't drift apart."""
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

    # Guard show_archived server-side too, so a stale date filter can't empty the active table.
    from utils.validators import validate_date_string

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
        from flask import jsonify

        return None, (jsonify({"error": f"sort_by must be one of: {', '.join(SORTABLE_COLUMNS)}"}), 400)
    sort_dir = args.get("sort_dir", "desc").lower()
    if sort_dir not in ("asc", "desc"):
        from flask import jsonify

        return None, (jsonify({"error": "sort_dir must be 'asc' or 'desc'"}), 400)
    sort_column = SORTABLE_COLUMNS[sort_by]

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

    return {
        "assets": assets,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_archived": show_archived,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }, None


def get_asset_stat_cards(conn):
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
    return {
        "stat_all": counts["all_count"],
        "stat_available": counts["available_count"] or 0,
        "stat_borrowed": counts["borrowed_count"] or 0,
        "stat_maintenance": counts["maintenance_count"] or 0,
    }


# # # CATEGORIES (lookup table) -------------------------------------------


def list_categories(conn):
    return conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()


def get_categories_with_asset_count(conn):
    # asset_count skips archived assets, so an all-archived category still looks deletable.
    return conn.execute(
        """
        SELECT categories.*, COUNT(assets.id) AS asset_count
        FROM categories
        LEFT JOIN assets ON assets.category_id = categories.id AND assets.deleted_at IS NULL
        GROUP BY categories.id
        ORDER BY categories.sort_order, categories.name
        """
    ).fetchall()


def add_category(conn, name, staff_id):
    name = (name or "").strip()
    if not name:
        return None, _no_ctx({"error": "Missing required field: name"}, 400)

    try:
        # New categories go last; COALESCE handles the first-ever category with no sort_order yet.
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
        return None, _no_ctx({"error": "A category with this name already exists"}, 400)

    log_action(
        staff_id=staff_id,
        action="created_category",
        target_table="categories",
        target_id=new_id,
        target_name=name,
        details=f"Created category: {name}",
    )

    return {"id": new_id, "name": name}, None


def edit_category(conn, category_id, name, staff_id):
    name = (name or "").strip()
    if not name:
        return None, _no_ctx({"error": "Missing required field: name"}, 400)

    category = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    if category is None:
        return None, _no_ctx({"error": "Category not found"}, 404)

    try:
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return None, _no_ctx({"error": "A category with this name already exists"}, 400)

    log_action(
        staff_id=staff_id,
        action="renamed_category",
        target_table="categories",
        target_id=category_id,
        target_name=name,
        details=f"Renamed category '{category['name']}' to '{name}'",
    )

    return {"message": "Category renamed successfully"}, None


def reorder_categories(conn, ordered_ids, staff_id):
    """Saves category order from the Manage Categories modal. Mirrors reorder_departments."""
    if len(set(ordered_ids)) != len(ordered_ids):
        return None, _no_ctx({"error": "order contains duplicate category ids"}, 400)

    existing_ids = {row["id"] for row in conn.execute("SELECT id FROM categories").fetchall()}

    if set(ordered_ids) != existing_ids:
        return None, _no_ctx({"error": "order must include exactly the current set of categories"}, 400)

    try:
        for position, cat_id in enumerate(ordered_ids):
            conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (position, cat_id))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Failed to save category order")
        return None, _no_ctx({"error": "Failed to save category order"}, 500)

    log_action(
        staff_id=staff_id,
        action="reordered_categories",
        target_table="categories",
        target_id=None,
        details=f"Reordered categories: {', '.join(str(i) for i in ordered_ids)}",
    )

    return {"message": "Category order saved successfully"}, None


def delete_category(conn, category_id, staff_id):
    """Blocks deleting a category with active assets; asks for reassignment first."""
    category = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    if category is None:
        return None, _no_ctx({"error": "Category not found"}, 404)

    asset_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM assets WHERE category_id = ? AND deleted_at IS NULL",
        (category_id,),
    ).fetchone()["cnt"]
    if asset_count > 0:
        noun = "asset" if asset_count == 1 else "assets"
        return None, _no_ctx(
            {
                "error": f"Can't delete \"{category['name']}\" -- {asset_count} {noun} still assigned to it. Reassign them to another category first."
            },
            400,
        )

    try:
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return None, _no_ctx(
            {"error": "This category can't be deleted because assets are still assigned to it"}, 400
        )

    log_action(
        staff_id=staff_id,
        action="deleted_category",
        target_table="categories",
        target_id=category_id,
        target_name=category["name"],
        details=f"Deleted category: {category['name']}",
    )

    return {"message": "Category deleted successfully"}, None


# # # CREATE ------------------------------------------------------------


def create_asset(conn, data, image_file, additional_image_files, staff_id):
    """Inserts a new asset; asset_id/barcode_id auto-generate as AST#####.
    Images are validated by the route before this is called; this function
    does the actual save-to-disk + DB write, rolling back and cleaning up
    saved files together if any step fails."""
    error = _validate_category_id(conn, data.get("category_id") or None)
    if error:
        return None, _no_ctx(*error)

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

        if image_file is not None:
            saved_image_path, img_error = save_asset_image(image_file, generated_code)
            if img_error:
                # Roll back the whole submission rather than saving the asset without its image.
                conn.rollback()
                return None, _no_ctx({"error": img_error}, 200)
            conn.execute(
                "UPDATE assets SET image_path = ? WHERE id = ?", (saved_image_path, new_asset_id)
            )

        for slot, file in additional_image_files.items():
            saved_path, img_error = save_asset_image(file, f"{generated_code}-alt{slot}")
            if img_error:
                # Same reasoning: don't leave the asset half-created with only some images attached.
                conn.rollback()
                if saved_image_path:
                    delete_asset_image(saved_image_path)
                for p in saved_additional_paths.values():
                    delete_asset_image(p)
                return None, _no_ctx({"error": img_error}, 200)
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
        return None, _no_ctx(
            {"error": "A database constraint was violated. Check for duplicate barcode/serial/ID values."}, 400
        )

    log_action(
        staff_id=staff_id,
        action="created_asset",
        target_table="assets",
        target_id=new_asset_id,
        target_name=f"{generated_code} — {data['name']}",
        details=f"Created asset {generated_code}",
    )

    return {"id": new_asset_id, "asset_id": generated_code}, None


# # # BULK IMPORT (CSV) ------------------------------------------------------


def _parse_import_deleted_at(raw):
    """Row is archived if deleted_at is non-blank. Returns None on unparseable so it errors, not un-archives."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def import_assets_csv(content, mode, staff_id):
    """Bulk-adds assets from CSV; 'preview' rolls back, 'commit' keeps it. Opens its own
    connection because it needs raw isolation_level/SAVEPOINT control the shared conn doesn't offer."""
    dry_run = mode == "preview"

    reader = csv.DictReader(io.StringIO(content))
    raw_fieldnames = reader.fieldnames or []

    # Accepts snake_case headers or Export's human-readable labels, so re-importing our export just works.
    reverse_labels = {label.lower(): key for key, label in EXPORT_FIELD_LABELS.items()}
    header_map = {}
    for h in raw_fieldnames:
        normalized = (h or "").strip()
        header_map[h] = reverse_labels.get(normalized.lower(), normalized.lower().replace(" ", "_"))
    canonical_fieldnames = list(header_map.values())

    missing_cols = [c for c in REQUIRED_CSV_COLUMNS if c not in canonical_fieldnames]
    if missing_cols:
        return None, _no_ctx({"error": f"CSV missing required column(s): {', '.join(missing_cols)}"}, 400)

    conn = get_db_connection()
    # isolation_level=None stops sqlite3 from auto-committing before our raw SAVEPOINT/ROLLBACK.
    conn.isolation_level = None
    conn.execute("BEGIN")
    created, errors, new_categories = [], [], []
    seen_category_names = set()

    try:
        _import_assets_rows(reader, header_map, conn, staff_id, created, errors, new_categories, seen_category_names)
        # Preview runs every row for real, then discards the batch to match a real commit.
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
    except Exception:
        # Anything past a per-row IntegrityError (already caught below) is unexpected --
        # roll back the still-open transaction so a bug here can't leave the DB write-locked.
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    archived_count = sum(1 for c in created if c["archived"])

    if dry_run:
        # No log_action here -- nothing was actually persisted, rolled back above.
        return {
            "dry_run": True,
            "rows_to_create": len(created),
            "rows_to_archive": archived_count,
            "errors": errors,
            "new_categories": new_categories,
        }, None

    detail_suffix = f" ({archived_count} archived)" if archived_count else ""
    log_action(
        staff_id=staff_id,
        action="bulk_imported_assets",
        target_table="assets",
        details=f"Imported {len(created)} asset(s) via CSV{detail_suffix}, {len(errors)} error(s)",
    )

    return {
        "dry_run": False,
        "created_count": len(created),
        "created": created,
        "created_ids": [c["id"] for c in created],
        "errors": errors,
        "archived_count": archived_count,
    }, None


def _import_assets_rows(reader, header_map, conn, staff_id, created, errors, new_categories, seen_category_names):
    """The per-row body of import_assets_csv(), split out so the outer function's
    try/finally can guarantee conn.close() runs no matter what happens in here."""
    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {header_map[h]: v for h, v in raw_row.items() if h in header_map}
        name = (row.get("name") or "").strip()
        # barcode_id is auto-generated from asset_id, same as single Add; any CSV column is ignored.
        if not name:
            errors.append({"row": row_num, "error": "name is required"})
            continue

        date_error = validate_date_fields(row, ("acquired_date", "last_calibrated"))
        if date_error:
            errors.append({"row": row_num, "error": "invalid date format"})
            continue

        # Blank deleted_at = active, non-blank = archived; see _parse_import_deleted_at.
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
                    staff_id if deleted_at is not None else None,
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
                {"row": row_num, "id": new_id, "barcode_id": generated_code, "archived": deleted_at is not None}
            )
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK TO row_import")
            conn.execute("RELEASE row_import")
            errors.append({"row": row_num, "error": f"duplicate or constraint violation: {e}"})


# # # EXPORT --------------------------------------------------------------


def default_export_basename(show_archived):
    return f"assets_{'archived' if show_archived else 'active'}_{datetime.now().strftime('%Y%m%d')}"


def resolve_export_columns(show_archived, selected):
    available_columns = list(EXPORT_CSV_COLUMNS)
    if show_archived:
        available_columns.append("deleted_at")

    if selected:
        columns = [c for c in available_columns if c in selected]
        if not columns:  # everything requested was invalid/unknown -- don't export a blank file
            columns = available_columns
    else:
        columns = available_columns
    return columns


def get_assets_for_export(conn, show_archived):
    where_clause = "assets.deleted_at IS NOT NULL" if show_archived else "assets.deleted_at IS NULL"
    return conn.execute(
        f"""
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE {where_clause}
        ORDER BY assets.asset_id
        """
    ).fetchall()


def build_assets_export_file(assets, columns, export_format, show_archived):
    """Returns (file_bytes, content_type, export_format)."""
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

        # Autosize columns to content; Excel would otherwise open them all at a narrow default.
        for i, col in enumerate(columns, start=1):
            longest = max([len(col)] + [len(str(asset[col] or "")) for asset in assets])
            ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 40)
        ws.freeze_panes = "A2"

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([EXPORT_FIELD_LABELS.get(col, col) for col in columns])
    for asset in assets:
        writer.writerow([asset[col] if asset[col] is not None else "" for col in columns])
    # BOM so Excel opens accented characters correctly on double-click.
    return output.getvalue().encode("utf-8-sig"), "text/csv", "csv"


def save_export_copy(file_bytes, save_dir, filename):
    """Best-effort server-side copy of an export; returns (saved_path_or_None, warning_or_None)."""
    try:
        os.makedirs(save_dir, exist_ok=True)
        saved_path = os.path.join(save_dir, filename)
        with open(saved_path, "wb") as f:
            f.write(file_bytes)
        return saved_path, None
    except OSError as e:
        logger.warning(f"Could not save export copy to disk: {e}")
        return None, "Could not save a copy on the server; your download still completed."


def list_export_subfolders(app_root, requested_path):
    """Lists subfolders under app_root for the export folder picker; clamped like the save-path."""
    from utils.exports import resolve_within_root

    resolved, _ = resolve_within_root(requested_path, app_root)

    if not os.path.isdir(resolved):
        resolved = app_root

    folders = sorted(
        entry
        for entry in os.listdir(resolved)
        if os.path.isdir(os.path.join(resolved, entry)) and not entry.startswith(".")
    )

    def _rel(p):
        rel = os.path.relpath(p, app_root)
        return "" if rel == "." else rel.replace(os.sep, "/")

    parent_path = None
    if resolved != app_root:
        parent_path = _rel(os.path.dirname(resolved))

    return {"path": _rel(resolved), "parent_path": parent_path, "folders": folders}


def create_export_subfolder(app_root, requested_path, name):
    from utils.exports import resolve_within_root

    resolved, _ = resolve_within_root(requested_path, app_root)
    if not os.path.isdir(resolved):
        resolved = app_root

    name = (name or "").strip()
    if not name or not _FOLDER_NAME_RE.match(name):
        return None, "Folder name can only contain letters, numbers, hyphens, and underscores"

    new_dir = os.path.join(resolved, name)
    os.makedirs(new_dir, exist_ok=True)
    return os.path.relpath(new_dir, app_root).replace(os.sep, "/"), None


# # # SINGLE ASSET (view/edit/delete/restore) ------------------------------


def get_asset(conn, asset_id):
    asset = conn.execute(
        """
        SELECT assets.*, categories.name AS category_name
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE assets.id = ? AND assets.deleted_at IS NULL
        """,
        (asset_id,),
    ).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    return dict(asset), None


def get_asset_for_edit(conn, asset_id):
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    categories = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    additional_images = fetch_asset_images(asset_id, conn)
    return {"asset": asset, "categories": categories, "additional_images": additional_images}, None


def update_asset(conn, asset_id, data, image_file, remove_image, additional_image_updates, swap_requested, staff_id):
    """additional_image_updates: {slot: {"file": FileStorage_or_None, "remove": bool}} from the route.
    Returns (data, error) where data on success is {"asset": asset_row, "condition": new_condition}."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)

    if asset["deleted_at"] is not None:
        return None, (
            {"error": "Cannot edit a deleted asset. Restore it first via /assets/<id>/restore."},
            400,
            {"asset": asset},
        )

    if "category_id" in data:
        error = _validate_category_id(conn, data.get("category_id"))
        if error:
            # Matches the original route: this specific failure always returns raw JSON,
            # even on non-JSON (htmx/form) requests -- no modal re-render.
            return None, _no_ctx(*error)

    # Image: replace, remove, or leave untouched (the common default case).
    has_new_image = image_file is not None
    new_image_path = asset["image_path"]
    old_image_to_delete = None

    if has_new_image:
        img_error = validate_image_upload(image_file)
        if img_error:
            return None, ({"error": img_error}, 400, {"asset": asset})
    elif remove_image and asset["image_path"]:
        old_image_to_delete = asset["image_path"]
        new_image_path = None

    # Additional images: same three outcomes, per fixed slot (1, 2).
    existing_additional_images = fetch_asset_images(asset_id, conn)
    additional_actions = {}

    for slot in ADDITIONAL_IMAGE_SLOTS:
        upd = additional_image_updates.get(slot, {})
        file = upd.get("file")
        remove_flag = upd.get("remove", False)
        existing_row = existing_additional_images.get(slot)

        if file is not None:
            img_error = validate_image_upload(file)
            if img_error:
                return None, ({"error": img_error}, 400, {"asset": asset})
            additional_actions[slot] = {
                "action": "replace",
                "file": file,
                "old_path": existing_row["image_path"] if existing_row else None,
            }
        elif remove_flag and existing_row is not None:
            additional_actions[slot] = {"action": "remove", "old_path": existing_row["image_path"]}

    # "Set as Main" swaps slots, unless either side is also being replaced/removed this submit.
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
        return None, _no_ctx(
            {
                "error": f"Cannot set status to '{new_status}' directly",
                "reason": "This status is only set by the checkout process, not by editing the asset.",
            },
            400,
        )

    if new_status != asset["status"] and asset["status"] in ("borrowed", "reserved"):
        return None, _no_ctx(
            {
                "error": f"Cannot change status: asset is currently {asset['status']}",
                "reason": "This asset is out with a member or in an active checkout cart. It must be returned or the cart cancelled before its status can be changed.",
            },
            409,
        )

    if new_condition == "disposed" and asset["status"] in ("borrowed", "reserved"):
        return None, _no_ctx(
            {
                "error": "Cannot dispose asset: it is currently borrowed",
                "reason": "This asset has an open borrow transaction. Return it first, or contact an admin.",
            },
            409,
        )

    saved_additional_paths = {}
    old_additional_to_delete = []

    try:
        if has_new_image:
            # Reuses the asset code as prefix; a random suffix stops it overwriting the old file.
            saved_path, img_error = save_asset_image(image_file, asset["asset_id"])
            if img_error:
                return None, ({"error": img_error}, 400, {"asset": asset})
            old_image_to_delete = asset["image_path"]
            new_image_path = saved_path

        for slot, act in additional_actions.items():
            if act["action"] == "replace":
                saved_path, img_error = save_asset_image(act["file"], f"{asset['asset_id']}-alt{slot}")
                if img_error:
                    return None, ({"error": img_error}, 400, {"asset": asset})
                saved_additional_paths[slot] = saved_path
                if act["old_path"]:
                    old_additional_to_delete.append(act["old_path"])
                conn.execute(
                    """
                    INSERT INTO asset_images (asset_id, slot, image_path) VALUES (?, ?, ?)
                    ON CONFLICT(asset_id, slot) DO UPDATE SET image_path = excluded.image_path
                    """,
                    (asset_id, slot, saved_path),
                )
            elif act["action"] == "remove":
                old_additional_to_delete.append(act["old_path"])
                conn.execute("DELETE FROM asset_images WHERE asset_id = ? AND slot = ?", (asset_id, slot))
            elif act["action"] == "swap":
                # Just moving pointers between slots -- nothing replaced or discarded, so nothing to delete.
                if act["new_path"]:
                    conn.execute(
                        """
                        INSERT INTO asset_images (asset_id, slot, image_path) VALUES (?, ?, ?)
                        ON CONFLICT(asset_id, slot) DO UPDATE SET image_path = excluded.image_path
                        """,
                        (asset_id, slot, act["new_path"]),
                    )
                else:
                    conn.execute("DELETE FROM asset_images WHERE asset_id = ? AND slot = ?", (asset_id, slot))

        set_clause = """
            SET name = ?, barcode_id = ?, rfid_id = ?, category_id = ?, condition = ?, status = ?, notes = ?, acquired_date = ?, last_calibrated = ?, image_path = ?
        """
        values = [
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
        ]
        if new_condition == "disposed":
            set_clause += ", deleted_at = datetime('now', 'localtime')"
        conn.execute(f"UPDATE assets {set_clause} WHERE id = ?", (*values, asset["id"]))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        if has_new_image and new_image_path:
            # The DB write failed after the file hit disk; clean it up, not an orphan.
            delete_asset_image(new_image_path)
        for p in saved_additional_paths.values():
            delete_asset_image(p)
        logger.error(f"IntegrityError: {e}")
        return None, (
            {"error": "A database constraint was violated. Check for duplicate barcode/RFID/serial values."},
            400,
            {"asset": asset},
        )

    if old_image_to_delete:
        # Only reached after a successful commit, so it's safe to delete the now-unreferenced file.
        delete_asset_image(old_image_to_delete)
    for p in old_additional_to_delete:
        delete_asset_image(p)

    updated_name = data.get("name", asset["name"])
    log_action(
        staff_id=staff_id,
        action="updated_asset",
        target_table="assets",
        target_id=asset["id"],
        # Mirrors the UPDATE's own fallback, so this reflects the name actually saved.
        target_name=f"{asset['asset_id']} — {updated_name}",
        details=f"Updated asset {asset['asset_id']}",
    )

    if new_condition == "disposed":
        log_action(
            staff_id=staff_id,
            action="disposed_asset",
            target_table="assets",
            target_id=asset["id"],
            target_name=f"{asset['asset_id']} — {updated_name}",
            details=f"Asset {asset['asset_id']} disposed on return and removed from circulation",
        )

    return {"message": "Asset updated successfully"}, None


def get_asset_for_delete(conn, asset_id):
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    return asset, None


def delete_asset(conn, asset_id, staff_id):
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)

    if asset["deleted_at"] is not None:
        return None, ({"error": "Asset is already deleted"}, 400, {"asset": asset})

    if asset["status"] in ("borrowed", "reserved"):
        return None, (
            {
                "error": "Cannot delete asset: it is currently borrowed",
                "reason": "This asset has an open borrow transaction. Return it first, or contact an admin.",
            },
            409,
            {"asset": asset, "plain_message": "Cannot delete asset: it is currently borrowed. Return it first, or contact an admin."},
        )

    deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "UPDATE assets SET deleted_at = ?, deleted_by = ? WHERE id = ?", (deleted_at, staff_id, asset_id)
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, (
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."},
            400,
            {"asset": asset},
        )

    log_action(
        staff_id=staff_id,
        action="deleted_asset",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} — {asset['name']}",
        details=f"Soft-deleted asset {asset['asset_id']}",
    )

    return {"message": "Asset soft deleted successfully", "asset_id_code": asset["asset_id"]}, None


def undo_delete_asset(conn, asset_id, staff_id):
    """Undo button on the delete toast; admin/in_charge like delete, no condition prompt needed."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)

    if asset["deleted_at"] is None:
        return None, _no_ctx({"error": "Asset is not deleted"}, 400)

    try:
        conn.execute(
            "UPDATE assets SET deleted_at = NULL, deleted_by = NULL, status = 'available' WHERE id = ?",
            (asset_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        staff_id=staff_id,
        action="undid_asset_delete",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} — {asset['name']}",
        details=f"Undid delete of asset {asset['asset_id']}",
    )

    return {"message": "Delete undone"}, None


def bulk_delete_assets(conn, ids, staff_id):
    """Soft-deletes multiple assets; a bad row goes into `skipped` instead of failing the whole batch."""
    deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    deleted, skipped = [], []

    for asset_id in ids:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

        if asset is None:
            skipped.append({"id": asset_id, "reason": "Asset not found"})
            continue

        if asset["deleted_at"] is not None:
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "Already deleted"})
            continue

        if asset["status"] in ("borrowed", "reserved"):
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "Currently borrowed or reserved"})
            continue

        try:
            conn.execute(
                "UPDATE assets SET deleted_at = ?, deleted_by = ? WHERE id = ?", (deleted_at, staff_id, asset_id)
            )
            conn.commit()
            deleted.append({"id": asset_id, "asset_id": asset["asset_id"]})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk delete of asset {asset_id}: {e}")
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "A database constraint was violated"})

    if deleted:
        log_action(
            staff_id=staff_id,
            action="bulk_deleted_assets",
            target_table="assets",
            details=(
                f"Bulk soft-deleted {len(deleted)} asset(s): "
                + ", ".join(a["asset_id"] for a in deleted)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return {"deleted_count": len(deleted), "deleted": deleted, "skipped": skipped}, None


def bulk_restore_assets(conn, ids, staff_id):
    """Restores multiple archived assets, admin-only. Disposed ones are skipped -- no per-row condition UI."""
    restored, skipped = [], []

    for asset_id in ids:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

        if asset is None:
            skipped.append({"id": asset_id, "reason": "Asset not found"})
            continue

        if asset["deleted_at"] is None:
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "Not archived"})
            continue

        if asset["condition"] == "disposed":
            skipped.append(
                {"id": asset_id, "asset_id": asset["asset_id"], "reason": "Disposed -- restore individually to choose a new condition"}
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
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "A database constraint was violated"})

    if restored:
        log_action(
            staff_id=staff_id,
            action="bulk_restored_assets",
            target_table="assets",
            details=(
                f"Bulk restored {len(restored)} asset(s): "
                + ", ".join(a["asset_id"] for a in restored)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return {"restored_count": len(restored), "restored": restored, "skipped": skipped}, None


def get_asset_for_restore(conn, asset_id):
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    if asset["deleted_at"] is None:
        return None, _no_ctx({"error": "Asset is not deleted"}, 400)
    return asset, None


def restore_asset(conn, asset_id, condition, reason, staff_id):
    """Admin-only, deliberately tighter than delete_asset (admin/in_charge)
    -- reversing a disposal is a bigger call than making one. Password check
    happens in the route (it needs g.staff, which is Flask-bound)."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)

    if asset["deleted_at"] is None:
        return None, ({"error": "Asset is not deleted"}, 400, {"asset_id": asset_id})

    if asset["condition"] == "disposed" and condition is None:
        return None, (
            {"error": "This asset's condition is 'disposed' -- choose a new condition to restore it"},
            400,
            {"asset_id": asset_id},
        )

    new_condition = condition if condition is not None else asset["condition"]

    try:
        conn.execute(
            "UPDATE assets SET deleted_at = NULL, deleted_by = NULL, condition = ?, status = 'available' WHERE id = ?",
            (new_condition, asset_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        staff_id=staff_id,
        action="restored_asset",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} — {asset['name']}",
        details=f"Restored asset {asset['asset_id']} (condition set to '{new_condition}') -- reason: {reason}",
    )

    return {"message": "Asset restored successfully", "asset_id_code": asset["asset_id"]}, None


def get_asset_by_barcode(conn, barcode):
    asset = conn.execute(
        """
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE (UPPER(assets.barcode_id) = UPPER(?) OR UPPER(assets.rfid_id) = UPPER(?)) AND assets.deleted_at IS NULL
        """,
        (barcode, barcode),
    ).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    return dict(asset), None


def fetch_asset_history(asset_id, limit, offset, conn):
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


def get_asset_history(conn, asset_id, limit, offset):
    asset = conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)

    total_count, history = fetch_asset_history(asset_id, limit, offset, conn)
    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "results": [dict(row) for row in history],
    }, None


def get_asset_history_modal_data(conn, asset_id, limit, offset):
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)

    total_count, history = fetch_asset_history(asset_id, limit, offset, conn)
    return {
        "asset": asset,
        "history": history,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }, None


def find_asset_id_by_barcode(conn, barcode):
    """Exact-match barcode lookup for the search box's Enter key."""
    row = conn.execute("SELECT id FROM assets WHERE UPPER(barcode_id) = UPPER(?)", (barcode,)).fetchone()
    return row["id"] if row else None


def get_asset_details(conn, asset_id):
    """Read-only 'Details' modal snapshot; adds current borrower/due date that the other modals skip."""
    asset = conn.execute(
        """
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE assets.id = ?
        """,
        (asset_id,),
    ).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)

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
            (asset_id,),
        ).fetchone()

    history_total_count, recent_history = fetch_asset_history(asset_id, DETAILS_RECENT_ACTIVITY_LIMIT, 0, conn)
    additional_images = fetch_asset_images(asset_id, conn)

    return {
        "asset": asset,
        "current_loan": current_loan,
        "recent_history": recent_history,
        "history_total_count": history_total_count,
        "additional_images": additional_images,
    }, None
