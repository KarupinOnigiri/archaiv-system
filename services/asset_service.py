"""Query-building ug mutation logic para sa routes/assets.py (assets +
categories).

Same convention sa services/member_service.py: matag function mo-dawat ug
bukas nga `conn` (ang route ang mo-open/close, gawas sa import_assets_csv
nga naa kaugalingong isolation-level control), mobalik ug `(data, error)`.
`error` None kung malampuson, o `(error_payload, status, context)` kung
napakyas - `error_payload` plain dict (para sa jsonify), `context` naay
dugang objects (e.g. {"asset": asset}) nga gikinahanglan sa route para
i-rerender ang inline-error template sa non-JSON nga request.
"""

import csv
import io
import logging
import os
import re
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from database import get_db_connection, sqlite3
from services.settings_service import (
    get_asset_id_prefix,
    get_default_maintenance_interval_days,
    get_maintenance_reminder_lead_days,
    is_feature_enabled,
)
from utils.logger import log_action
from utils.uploads import save_asset_image, delete_asset_image, validate_image_upload
from utils.validators import validate_date_fields, normalize_barcode_id, decimal_scan_hex_suffix, escape_like_wildcards

logger = logging.getLogger(__name__)

ADDITIONAL_IMAGE_SLOTS = (1, 2)
VALID_CONDITIONS = ("functional", "inoperable", "for_repair", "disposed")
# 'serialized' (the original/default model - one row = one physical, uniquely
# barcoded/RFID-tagged tool) vs 'consumable' (a quantity-tracked stock item,
# e.g. "50x safety goggles" - no per-unit identity, checkout decrements
# quantity instead of flipping status to 'borrowed'). See services/
# transaction_service.py's add_item_to_checkout_session for the parallel
# checkout path this enables.
VALID_ASSET_TYPES = ("serialized", "consumable")
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
    """Wrapper para sa error tuple nga walay dugang context - kadaghanan
    sa mga error dinhi walay laing object nga gikinahanglan sa route."""
    return (error_payload, status, {})


def _ensure_category_sort_order_column():
    """Dugangan ang sort_order sa categories kung wala pa, backfilled
    alphabetically. Safe rerun-on."""
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
    """Mobalik ug {1: row_or_None, 2: row_or_None} para ang templates
    makaindex base sa slot number."""
    rows = conn.execute("SELECT * FROM asset_images WHERE asset_id = ?", (asset_id,)).fetchall()
    by_slot = {slot: None for slot in ADDITIONAL_IMAGE_SLOTS}
    for row in rows:
        by_slot[row["slot"]] = row
    return by_slot


def _validate_category_id(conn, category_id, required=False):
    """Local, non-Flask nga mirror sa
    utils.validators.validate_lookup_id(conn, "categories", ...)."""
    if category_id is None:
        if required:
            return {"error": "Missing required field: category_id"}, 400
        return None
    row = conn.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
    if row is None:
        return {"error": "category_id does not reference an existing entry"}, 400
    return None


def _validate_int(raw, field_name, allow_negative=False):
    """Shared int-field parser para sa quantity/reorder_threshold.

    Returns: tuple (value_or_None, error_message_or_None)."""
    if raw in (None, ""):
        return None, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"{field_name} must be a whole number"
    if not allow_negative and value < 0:
        return None, f"{field_name} cannot be negative"
    return value, None


def _validate_asset_type_fields(asset_type_raw, quantity_raw, reorder_threshold_raw):
    """Ang consumable nga asset kinahanglan gyud naay quantity - mao gyud
    ang punto niini, ang quantity ang nagsubay sa availability imbes usa
    ka status flip; ang reorder_threshold optional ra. Ang serialized nga
    asset (default) kanunay quantity/reorder_threshold None - dili
    makahulugan kining mga column para sa usa ka uniquely-tagged nga
    physical item, so hilom ra i-ignore ang bisan unsa nga stray value.

    Returns: tuple (asset_type, quantity, reorder_threshold, error)."""
    asset_type = asset_type_raw or "serialized"
    if asset_type not in VALID_ASSET_TYPES:
        return None, None, None, f"asset_type must be one of: {', '.join(VALID_ASSET_TYPES)}"

    if asset_type == "serialized":
        return asset_type, None, None, None

    quantity, error = _validate_int(quantity_raw, "quantity")
    if error:
        return None, None, None, error
    if quantity is None:
        return None, None, None, "quantity is required for a consumable asset"

    reorder_threshold, error = _validate_int(reorder_threshold_raw, "reorder_threshold")
    if error:
        return None, None, None, error

    return asset_type, quantity, reorder_threshold, None


def _default_next_maintenance_due(conn, acquired_date, last_calibrated):
    """Mag-auto-fill sa next_maintenance_due kung wala ni-set ang staff -
    last_calibrated + ang global default interval kung naa'y calibration
    date, kung wala, acquired_date + interval, kung wala pod, today +
    interval. Ang consumable dili mutawag ani - walay maintenance concept."""
    interval_days = get_default_maintenance_interval_days(conn)
    anchor = last_calibrated or acquired_date
    if anchor:
        anchor_date = anchor.split(" ")[0]  # tolerate a full timestamp, only the date part matters
        return conn.execute(
            "SELECT date(?, ? || ' days') AS d", (anchor_date, str(interval_days))
        ).fetchone()["d"]
    return conn.execute("SELECT date('now', 'localtime', ? || ' days') AS d", (str(interval_days),)).fetchone()["d"]


def _validate_purchase_cost(raw):
    """Optional nga purchase cost (gigamit sa Reports' Asset Valuation).
    Mirrors normalize_barcode_id's shape para pareho ra ang pattern sa
    caller. Blank/missing OK ra; negative o dili ma-parse i-reject.

    Returns: tuple (value_or_None, error_message_or_None)."""
    if raw in (None, ""):
        return None, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "purchase_cost must be a number"
    if value < 0:
        return None, "purchase_cost cannot be negative"
    return value, None


# # # LIST / VIEW ------------------------------------------------------------


def get_assets_table_context(conn, args, limit, offset):
    """Shared table context para sa view_assets() ug assets_page(), para
    dili sila mag-drift apart.

    Returns: dict {assets, total_count, limit, offset, ...} - tan-awa ang
    return statement sa ubos para sa tanang keys."""
    show_archived = args.get("show_archived") == "1"

    if show_archived:
        where_clauses = ["assets.deleted_at IS NOT NULL"]
    else:
        where_clauses = ["assets.deleted_at IS NULL"]
    params = []

    q = args.get("q")
    if q:
        where_clauses.append(
            "(assets.name LIKE ? ESCAPE '\\' OR assets.barcode_id LIKE ? ESCAPE '\\'"
            " OR assets.asset_id LIKE ? ESCAPE '\\' OR assets.rfid_id LIKE ? ESCAPE '\\')"
        )
        like = f"%{escape_like_wildcards(q)}%"
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

    # Deep-link filter for the Dashboard's "Maintenance Due" widget (its
    # own "View all" used to just point at the plain, unfiltered Inventory
    # page - there was no way to actually see the full list it summarizes).
    # Same due-detection as get_maintenance_due_assets below (date-based OR
    # usage-interval-based, both against the same global lead-days setting),
    # kept as inline SQL here rather than reusing that function directly
    # since this needs to compose with the table's other filters/pagination
    # rather than return its own separate row set.
    if args.get("maintenance_due") == "1":
        lead_days = get_maintenance_reminder_lead_days(conn)
        where_clauses.append(
            "assets.status != 'maintenance' AND ("
            "(assets.next_maintenance_due IS NOT NULL AND assets.next_maintenance_due <= date('now', 'localtime', ? || ' days')) "
            "OR (assets.maintenance_interval_checkouts IS NOT NULL AND assets.checkouts_since_maintenance >= assets.maintenance_interval_checkouts)"
            ")"
        )
        params.append(str(lead_days))

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
    # Only joined when the feature's on - it's off by default (migration
    # 0057) and this correlated subquery isn't free, no reason to pay for it
    # on every Inventory page load for installs that don't use booking.
    reservation_select = (
        """, (SELECT MIN(r.start_at) FROM reservations r
              WHERE r.asset_id = assets.id AND r.status = 'scheduled'
                AND r.end_at >= datetime('now', 'localtime')) AS next_reservation_start"""
        if is_feature_enabled("reservations", conn)
        else ""
    )
    assets = conn.execute(
        f"""
        SELECT assets.*, categories.name AS category, staff.username AS deleted_by_username{reservation_select}
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        LEFT JOIN staff ON assets.deleted_by = staff.id
        {where_sql}
        ORDER BY {sort_column} {sort_dir.upper()}, assets.id {sort_dir.upper()}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    # Passed through so _table.html can flag "due"/"due soon" with plain
    # string comparisons against next_maintenance_due (ISO dates compare
    # correctly lexicographically) instead of a per-row Python computation -
    # simplest way to keep get_assets_table_context returning bare sqlite3.Row
    # objects rather than converting every row to a dict just for this.
    today = datetime.now().strftime("%Y-%m-%d")
    lead_days = get_maintenance_reminder_lead_days(conn)
    maintenance_due_soon_cutoff = (datetime.now() + timedelta(days=lead_days)).strftime("%Y-%m-%d")

    return {
        "assets": assets,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_archived": show_archived,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "today": today,
        "maintenance_due_soon_cutoff": maintenance_due_soon_cutoff,
    }, None


def get_asset_stat_cards(conn):
    """Ang 4 ka numero sa Inventory page's stat cards (All/Available/
    Borrowed/Maintenance) dungan sa low-stock ug maintenance-due-soon
    counts.

    Returns: dict {stat_all, stat_available, stat_borrowed,
    stat_maintenance, stat_low_stock, stat_maintenance_due_soon}."""
    lead_days = get_maintenance_reminder_lead_days(conn)
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS all_count,
            SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) AS available_count,
            SUM(CASE WHEN status = 'borrowed' THEN 1 ELSE 0 END) AS borrowed_count,
            SUM(CASE WHEN status = 'maintenance' THEN 1 ELSE 0 END) AS maintenance_count,
            SUM(CASE WHEN asset_type = 'consumable' AND reorder_threshold IS NOT NULL
                       AND quantity <= reorder_threshold THEN 1 ELSE 0 END) AS low_stock_count,
            SUM(CASE WHEN status != 'maintenance' AND (
                    (next_maintenance_due IS NOT NULL AND next_maintenance_due <= date('now', 'localtime', ? || ' days'))
                    OR (maintenance_interval_checkouts IS NOT NULL AND checkouts_since_maintenance >= maintenance_interval_checkouts)
                ) THEN 1 ELSE 0 END) AS maintenance_due_soon_count
        FROM assets
        WHERE deleted_at IS NULL
        """,
        (str(lead_days),),
    ).fetchone()
    return {
        "stat_all": counts["all_count"],
        "stat_available": counts["available_count"] or 0,
        "stat_borrowed": counts["borrowed_count"] or 0,
        "stat_maintenance": counts["maintenance_count"] or 0,
        "stat_low_stock": counts["low_stock_count"] or 0,
        "stat_maintenance_due_soon": counts["maintenance_due_soon_count"] or 0,
    }


# # # CATEGORIES (lookup table) -------------------------------------------


def list_categories(conn):
    """Tanang categories, sorted, para sa dropdown/select options."""
    return conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()


def get_categories_with_asset_count(conn):
    """Categories dungan sa ilang asset_count - para sa Manage Categories
    modal, mao ni nag-decide kung deletable ba ang category."""
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
    """Mag-add ug bag-ong category - naay unique-name check ug auto
    sort_order (mo-last siya sa listahan)."""
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
        conn=conn,
        staff_id=staff_id,
        action="created_category",
        target_table="categories",
        target_id=new_id,
        target_name=name,
        details=f"Created category: {name}",
    )

    return {"id": new_id, "name": name}, None


def edit_category(conn, category_id, name, staff_id):
    """Mag-rename ug category - naay unique-name check pud."""
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
        conn=conn,
        staff_id=staff_id,
        action="renamed_category",
        target_table="categories",
        target_id=category_id,
        target_name=name,
        details=f"Renamed category '{category['name']}' to '{name}'",
    )

    return {"message": "Category renamed successfully"}, None


def reorder_categories(conn, ordered_ids, staff_id):
    """Mag-save sa bag-ong category order gikan sa Manage Categories modal
    (drag-and-drop). Mirrors reorder_departments."""
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
        conn=conn,
        staff_id=staff_id,
        action="reordered_categories",
        target_table="categories",
        target_id=None,
        details=f"Reordered categories: {', '.join(str(i) for i in ordered_ids)}",
    )

    return {"message": "Category order saved successfully"}, None


def delete_category(conn, category_id, staff_id):
    """Mag-delete ug category, pero dili tugotan kung naay active assets pa
    - reassign una sa laing category before delete."""
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
                "error": f"Can't delete \"{category['name']}\" - {asset_count} {noun} still assigned to it. Reassign them to another category first."
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
        conn=conn,
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
    """Mag-insert ug bag-ong asset. Ang asset_id kanunay auto-generate as
    AST##### - ang barcode_id parehas ana gawas kung naay gi-supply na
    custom barcode (e.g. naa nay existing manufacturer tag). Images gi
    validate na sa route; dinhi na lang ang save-to-disk + DB write, ug
    mag-rollback + cleanup sa saved files tanan kung naay mapakyas."""
    error = _validate_category_id(conn, data.get("category_id") or None)
    if error:
        return None, _no_ctx(*error)

    custom_barcode, barcode_error = normalize_barcode_id(data.get("barcode_id"))
    if barcode_error:
        return None, _no_ctx({"error": barcode_error}, 400)

    if custom_barcode is not None:
        existing = conn.execute(
            "SELECT id FROM assets WHERE UPPER(barcode_id) = UPPER(?)", (custom_barcode,)
        ).fetchone()
        if existing:
            return None, _no_ctx(
                {"error": "This barcode is already registered to another asset."}, 400
            )

    purchase_cost, cost_error = _validate_purchase_cost(data.get("purchase_cost"))
    if cost_error:
        return None, _no_ctx({"error": cost_error}, 400)

    asset_type, quantity, reorder_threshold, type_error = _validate_asset_type_fields(
        data.get("asset_type"), data.get("quantity"), data.get("reorder_threshold")
    )
    if type_error:
        return None, _no_ctx({"error": type_error}, 400)

    maintenance_interval, interval_error = _validate_int(
        data.get("maintenance_interval_checkouts"), "maintenance_interval_checkouts"
    )
    if interval_error:
        return None, _no_ctx({"error": interval_error}, 400)

    # Consumables have no maintenance concept (asset_type docs); a
    # serialized asset with no explicit due date gets one auto-computed
    # from its calibration/acquisition date, so "no schedule was set" isn't
    # the silent default - see _default_next_maintenance_due.
    next_maintenance_due = data.get("next_maintenance_due") or None
    if asset_type == "serialized" and not next_maintenance_due:
        next_maintenance_due = _default_next_maintenance_due(
            conn, data.get("acquired_date"), data.get("last_calibrated")
        )

    new_asset_id = None
    generated_code = None
    saved_image_path = None
    saved_additional_paths = {}

    try:
        cursor = conn.execute(
            """
            INSERT INTO assets (asset_id, name, barcode_id, rfid_id, category_id, serial_code, acquired_date, last_calibrated, notes, purchase_cost, asset_type, quantity, reorder_threshold, next_maintenance_due, maintenance_interval_checkouts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                purchase_cost,
                asset_type,
                quantity,
                reorder_threshold,
                next_maintenance_due,
                maintenance_interval,
            ),
        )
        new_asset_id = cursor.lastrowid
        generated_code = f"{get_asset_id_prefix(conn)}{new_asset_id:05d}"
        conn.execute(
            "UPDATE assets SET asset_id = ?, barcode_id = ? WHERE id = ?",
            (generated_code, custom_barcode or generated_code, new_asset_id),
        )

        if image_file is not None:
            saved_image_path, img_error = save_asset_image(image_file, generated_code)
            if img_error:
                # Roll back the whole submission rather than saving the asset without its image.
                conn.rollback()
                return None, _no_ctx({"error": img_error}, 400)
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
                return None, _no_ctx({"error": img_error}, 400)
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
        if custom_barcode is not None:
            return None, _no_ctx(
                {"error": "This barcode is already registered to another asset."}, 400
            )
        return None, _no_ctx(
            {"error": "A database constraint was violated. Check for duplicate barcode/serial/ID values."}, 400
        )

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="created_asset",
        target_table="assets",
        target_id=new_asset_id,
        target_name=f"{generated_code} - {data['name']}",
        details=f"Created asset {generated_code}",
    )

    return {"id": new_asset_id, "asset_id": generated_code}, None


# # # BULK IMPORT (CSV) ------------------------------------------------------


def _parse_import_deleted_at(raw):
    """Archived na ang row kung naay laman ang deleted_at. Mobalik ug None
    kung dili ma-parse aron mo-error dayon, dili basta mag-unarchive."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def import_assets_csv(content, mode, staff_id):
    """Bulk-add ug assets gikan sa CSV - 'preview' mode mo-rollback ra,
    'commit' mo-save jud. Naay kaugalingong connection kay kinahanglan raw
    isolation_level/SAVEPOINT control nga wala sa shared conn."""
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
        # No log_action here - nothing was actually persisted, rolled back above.
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
    """Ang per-row logic sa import_assets_csv(), gi-separate para ang
    outer function's try/finally maka-guarantee nga mo-run gyud ang
    conn.close() bisan unsay mahitabo diri."""
    # Read once outside the loop - it can't change mid-import, so a 500-row
    # CSV shouldn't pay for 500 redundant app_settings lookups.
    id_prefix = get_asset_id_prefix(conn)
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
            generated_code = f"{id_prefix}{new_id:05d}"
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
    """Default filename para sa export - naay date stamp, ug "archived" o
    "active" depende sa gi-export."""
    return f"assets_{'archived' if show_archived else 'active'}_{datetime.now().strftime('%Y%m%d')}"


def resolve_export_columns(show_archived, selected):
    """Mag-filter sa gi-request nga columns batok sa allowed export set -
    mo-add ug deleted_at column kung archived assets ang gi-export."""
    available_columns = list(EXPORT_CSV_COLUMNS)
    if show_archived:
        available_columns.append("deleted_at")

    if selected:
        columns = [c for c in available_columns if c in selected]
        if not columns:  # everything requested was invalid/unknown - don't export a blank file
            columns = available_columns
    else:
        columns = available_columns
    return columns


def get_assets_for_export(conn, show_archived):
    """Kuha'on tanang assets nga i-export (active o archived), naay
    category name na dungan."""
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
    """Mag-build sa actual export file (xlsx o csv). Returns (file_bytes,
    content_type, export_format)."""
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
    """Best-effort server-side copy sa export file - kung mapakyas, dili
    mag-error total naa may nadownload na ang user. Returns
    (saved_path_or_None, warning_or_None)."""
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
    """Listahan sa subfolders ubos sa app_root para sa export folder
    picker - clamped gihapon sama sa save-path (dili makagawas sa app_root)."""
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
    """Mag-himo ug bag-ong subfolder para sa export destination - name
    validated batok sa safe-filename regex."""
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
    """Single active (non-deleted) asset dungan sa category name - para sa
    detail view."""
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
    """Asset (bisan deleted na) plus categories dropdown ug additional
    images - para sa Edit modal, mao ni ang gina-load pag-open sa form."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    categories = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    additional_images = fetch_asset_images(asset_id, conn)
    return {"asset": asset, "categories": categories, "additional_images": additional_images}, None


def update_asset(conn, asset_id, data, image_file, remove_image, additional_image_updates, swap_requested, staff_id):
    """Mag-update sa asset - naay dako nga logic para sa barcode change,
    image replace/remove/swap, ug consumable/serialized status rules.
    additional_image_updates: {slot: {"file": FileStorage_or_None, "remove":
    bool}} gikan sa route. Returns (data, error) - data on success kay
    {"asset": asset_row, "condition": new_condition}."""
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
            # even on non-JSON (htmx/form) requests - no modal re-render.
            return None, _no_ctx(*error)

    new_asset_type, new_quantity, new_reorder_threshold = asset["asset_type"], asset["quantity"], asset["reorder_threshold"]
    if "asset_type" in data or "quantity" in data or "reorder_threshold" in data:
        new_asset_type, new_quantity, new_reorder_threshold, type_error = _validate_asset_type_fields(
            data.get("asset_type", asset["asset_type"]),
            data.get("quantity", asset["quantity"]),
            data.get("reorder_threshold", asset["reorder_threshold"]),
        )
        if type_error:
            return None, ({"error": type_error}, 400, {"asset": asset})

    new_barcode_id = asset["barcode_id"]
    if "barcode_id" in data:
        custom_barcode, barcode_error = normalize_barcode_id(data.get("barcode_id"))
        if barcode_error:
            return None, ({"error": barcode_error}, 400, {"asset": asset})
        # Blank clears back to nothing meaningful to fall back to - barcode_id is
        # NOT NULL, so refuse rather than silently keeping the old value or erroring
        # obscurely at the UPDATE.
        if custom_barcode is None:
            return None, (
                {"error": "barcode_id cannot be blank - clear it back to the asset ID instead if needed."},
                400,
                {"asset": asset},
            )
        if custom_barcode.upper() != (asset["barcode_id"] or "").upper():
            existing = conn.execute(
                "SELECT id FROM assets WHERE UPPER(barcode_id) = UPPER(?) AND id != ?",
                (custom_barcode, asset_id),
            ).fetchone()
            if existing:
                return None, (
                    {"error": "This barcode is already registered to another asset."},
                    400,
                    {"asset": asset},
                )
        new_barcode_id = custom_barcode

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

    # Consumables never enter the serialized borrowed/reserved/maintenance
    # lifecycle - checkout decrements `quantity` instead (see
    # transaction_service.add_item_to_checkout_session), so a consumable's
    # status has no reason to ever be anything but 'available'.
    if new_asset_type == "consumable" and new_status != "available":
        return None, _no_ctx(
            {
                "error": f"Cannot set status to '{new_status}' on a consumable asset",
                "reason": "Consumables track availability via quantity, not status - they stay 'available' always.",
            },
            400,
        )

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

    new_purchase_cost = asset["purchase_cost"]
    if "purchase_cost" in data:
        new_purchase_cost, cost_error = _validate_purchase_cost(data.get("purchase_cost"))
        if cost_error:
            return None, ({"error": cost_error}, 400, {"asset": asset})

    new_next_maintenance_due = data.get("next_maintenance_due", asset["next_maintenance_due"])
    new_maintenance_interval = asset["maintenance_interval_checkouts"]
    if "maintenance_interval_checkouts" in data:
        new_maintenance_interval, interval_error = _validate_int(
            data.get("maintenance_interval_checkouts"), "maintenance_interval_checkouts"
        )
        if interval_error:
            return None, ({"error": interval_error}, 400, {"asset": asset})

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
                # Just moving pointers between slots - nothing replaced or discarded, so nothing to delete.
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
            SET name = ?, barcode_id = ?, rfid_id = ?, category_id = ?, condition = ?, status = ?, notes = ?, acquired_date = ?, last_calibrated = ?, image_path = ?, serial_code = ?, purchase_cost = ?, asset_type = ?, quantity = ?, reorder_threshold = ?, next_maintenance_due = ?, maintenance_interval_checkouts = ?
        """
        values = [
            data.get("name", asset["name"]),
            new_barcode_id,
            data.get("rfid_id", asset["rfid_id"]) or None,
            data.get("category_id", asset["category_id"]),
            new_condition,
            new_status,
            data.get("notes", asset["notes"]),
            data.get("acquired_date", asset["acquired_date"]) or None,
            data.get("last_calibrated", asset["last_calibrated"]) or None,
            new_image_path,
            # Used to be permanently locked after creation (a data-entry
            # mistake was uncorrectable without deleting and recreating the
            # asset) - now editable like barcode_id/rfid_id, same nullable-
            # unique constraint (idx_assets_serial_code) already caught
            # generically by the IntegrityError handler below.
            data.get("serial_code", asset["serial_code"]) or None,
            new_purchase_cost,
            new_asset_type,
            new_quantity,
            new_reorder_threshold,
            new_next_maintenance_due or None,
            new_maintenance_interval,
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
        conn=conn,
        staff_id=staff_id,
        action="updated_asset",
        target_table="assets",
        target_id=asset["id"],
        # Mirrors the UPDATE's own fallback, so this reflects the name actually saved.
        target_name=f"{asset['asset_id']} - {updated_name}",
        details=f"Updated asset {asset['asset_id']}",
    )

    if new_condition == "disposed":
        log_action(
            conn=conn,
            staff_id=staff_id,
            action="disposed_asset",
            target_table="assets",
            target_id=asset["id"],
            target_name=f"{asset['asset_id']} - {updated_name}",
            details=f"Asset {asset['asset_id']} disposed on return and removed from circulation",
        )

    return {"message": "Asset updated successfully"}, None


def get_asset_for_delete(conn, asset_id):
    """Asset row (bisan unsay status) para sa delete-confirmation modal."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    return asset, None


def delete_asset(conn, asset_id, staff_id):
    """Soft-delete ra (deleted_at timestamp) - dili tugotan kung borrowed
    o reserved pa ang asset."""
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
        conn=conn,
        staff_id=staff_id,
        action="deleted_asset",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=f"Soft-deleted asset {asset['asset_id']}",
    )

    return {"message": "Asset soft deleted successfully", "asset_id_code": asset["asset_id"]}, None


def undo_delete_asset(conn, asset_id, staff_id):
    """Undo button sa delete toast - i-unarchive dayon, walay condition
    prompt kay parehas ra sa delete permission (admin/in_charge)."""
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
        conn=conn,
        staff_id=staff_id,
        action="undid_asset_delete",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=f"Undid delete of asset {asset['asset_id']}",
    )

    return {"message": "Delete undone"}, None


def bulk_delete_assets(conn, ids, staff_id):
    """Soft-delete ug daghang assets dungan - kung naay bad row, mo-adto ra
    na sa `skipped` list, dili mo-fail ang tibuok batch."""
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
            conn=conn,
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
    """Mag-restore ug daghang archived assets, admin-only. Ang disposed
    kay laktawan - kinahanglan i-restore isa-isa para makapili ug bag-ong
    condition."""
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
                {"id": asset_id, "asset_id": asset["asset_id"], "reason": "Disposed - restore individually to choose a new condition"}
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
            conn=conn,
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


def bulk_reassign_category(conn, ids, category_id, staff_id):
    """Mag-move ug daghang assets ngadto sa laing category sa usa ka
    action - parehas partial-success shape sa
    bulk_delete_assets/bulk_restore_assets."""
    error = _validate_category_id(conn, category_id)
    if error:
        return None, _no_ctx(*error)

    reassigned, skipped = [], []

    for asset_id in ids:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

        if asset is None:
            skipped.append({"id": asset_id, "reason": "Asset not found"})
            continue
        if asset["deleted_at"] is not None:
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "Archived - restore it first"})
            continue

        try:
            conn.execute("UPDATE assets SET category_id = ? WHERE id = ?", (category_id, asset_id))
            conn.commit()
            reassigned.append({"id": asset_id, "asset_id": asset["asset_id"]})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk category reassign of asset {asset_id}: {e}")
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "A database constraint was violated"})

    if reassigned:
        category = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
        log_action(
            conn=conn,
            staff_id=staff_id,
            action="bulk_reassigned_category",
            target_table="assets",
            target_name=category["name"] if category else None,
            details=(
                f"Bulk reassigned {len(reassigned)} asset(s) to category '{category['name'] if category else category_id}': "
                + ", ".join(a["asset_id"] for a in reassigned)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return {"reassigned_count": len(reassigned), "reassigned": reassigned, "skipped": skipped}, None


BULK_STATUS_CHANGE_VALUES = ("available", "maintenance")


def bulk_change_status(conn, ids, new_status, staff_id):
    """Bulk status change - limited ra sa available<->maintenance (kining
    duha ra ang pwede i-edit by hand; borrowed/reserved kay checkout-process
    ra, tan-awa ang guard sa update_asset). Consumables ug bisan unsay
    currently borrowed/reserved kay laktawan, parehas rason sa
    bulk_delete_assets."""
    if new_status not in BULK_STATUS_CHANGE_VALUES:
        return None, _no_ctx(
            {"error": f"status must be one of: {', '.join(BULK_STATUS_CHANGE_VALUES)}"}, 400
        )

    changed, skipped = [], []

    for asset_id in ids:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

        if asset is None:
            skipped.append({"id": asset_id, "reason": "Asset not found"})
            continue
        if asset["deleted_at"] is not None:
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "Archived - restore it first"})
            continue
        if asset["asset_type"] == "consumable":
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "Consumables don't have a status"})
            continue
        if asset["status"] in ("borrowed", "reserved"):
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": f"Currently {asset['status']}"})
            continue
        if asset["status"] == new_status:
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": f"Already {new_status}"})
            continue

        try:
            conn.execute("UPDATE assets SET status = ? WHERE id = ?", (new_status, asset_id))
            conn.commit()
            changed.append({"id": asset_id, "asset_id": asset["asset_id"]})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk status change of asset {asset_id}: {e}")
            skipped.append({"id": asset_id, "asset_id": asset["asset_id"], "reason": "A database constraint was violated"})

    if changed:
        log_action(
            conn=conn,
            staff_id=staff_id,
            action="bulk_changed_status",
            target_table="assets",
            target_name=new_status,
            details=(
                f"Bulk set {len(changed)} asset(s) to '{new_status}': "
                + ", ".join(a["asset_id"] for a in changed)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return {"changed_count": len(changed), "changed": changed, "skipped": skipped}, None


def get_asset_for_restore(conn, asset_id):
    """Deleted asset row para sa Restore modal (naay condition picker kung
    disposed)."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    if asset["deleted_at"] is None:
        return None, _no_ctx({"error": "Asset is not deleted"}, 400)
    return asset, None


def restore_asset(conn, asset_id, condition, reason, staff_id):
    """Admin-only, mas tighter pa sa delete_asset (admin/in_charge ra) -
    ang pag-undo ug disposal mas dako na desisyon kaysa pag-himo ana.
    Password check kay sa route na (kinahanglan g.staff, Flask-bound)."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)

    if asset["deleted_at"] is None:
        return None, ({"error": "Asset is not deleted"}, 400, {"asset_id": asset_id})

    if asset["condition"] == "disposed" and condition is None:
        return None, (
            {"error": "This asset's condition is 'disposed' - choose a new condition to restore it"},
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
        conn=conn,
        staff_id=staff_id,
        action="restored_asset",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=f"Restored asset {asset['asset_id']} (condition set to '{new_condition}') - reason: {reason}",
    )

    return {"message": "Asset restored successfully", "asset_id_code": asset["asset_id"]}, None


# # # MARK CALIBRATED (quick action, no full Edit modal needed) -----------
# Standalone from resolve_maintenance below (which also updates
# last_calibrated, but only as a side effect of fixing a maintenance-status
# asset) - this is for logging a calibration on its own, any time, without
# the asset needing to be in maintenance at all.


def get_asset_for_mark_calibrated(conn, asset_id):
    """Validation-only lookup before mark_calibrated - dili tugotan kung
    deleted na o consumable (walay calibration date)."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    if asset["deleted_at"] is not None:
        return None, _no_ctx({"error": "Cannot calibrate a deleted asset"}, 400)
    if asset["asset_type"] == "consumable":
        return None, _no_ctx({"error": "Consumables don't have a calibration date"}, 400)
    return asset, None


def mark_calibrated(conn, asset_id, staff_id):
    """Mag-set sa last_calibrated to today, ug parehas sa
    resolve_maintenance, mag-recompute sa next_maintenance_due base sa
    bag-ong date + global default interval - ang pag-calibrate mo-reset sa
    preventive-maintenance clock, parehas ra sa pag-ayo gikan sa
    maintenance."""
    asset, error = get_asset_for_mark_calibrated(conn, asset_id)
    if error:
        error_payload, status, _ctx = error
        return None, (error_payload, status, {"asset": conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()})

    today = conn.execute("SELECT date('now', 'localtime') AS d").fetchone()["d"]
    next_maintenance_due = _default_next_maintenance_due(conn, asset["acquired_date"], today)

    try:
        conn.execute(
            "UPDATE assets SET last_calibrated = ?, next_maintenance_due = ? WHERE id = ?",
            (today, next_maintenance_due, asset_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, ({"error": "A database constraint was violated."}, 400, {"asset": asset})

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="marked_calibrated",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=f"Calibrated {asset['asset_id']} on {today}",
    )

    return {"message": "Asset marked as calibrated", "asset_id_code": asset["asset_id"]}, None


# # # MARK FOR MAINTENANCE (quick action, no full Edit modal needed) -------
# Inverse of resolve_maintenance below - lets staff flag an asset as needing
# service directly from the row menu/Details modal, without opening Edit
# and hand-picking status='maintenance' from the dropdown there.


def get_asset_for_mark_maintenance(conn, asset_id):
    """Validation-only lookup before mark_for_maintenance - dili tugotan
    kung deleted, consumable, borrowed/reserved, o maintenance na diay."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    if asset["deleted_at"] is not None:
        return None, _no_ctx({"error": "Cannot mark a deleted asset for maintenance"}, 400)
    if asset["asset_type"] == "consumable":
        return None, _no_ctx({"error": "Consumables don't have a maintenance status"}, 400)
    if asset["status"] in ("borrowed", "reserved"):
        return None, _no_ctx(
            {"error": f"Cannot mark this asset for maintenance - it's currently {asset['status']}"}, 409
        )
    if asset["status"] == "maintenance":
        return None, _no_ctx({"error": "Asset is already in maintenance"}, 400)
    return asset, None


def mark_for_maintenance(conn, asset_id, notes, staff_id):
    """Quick action: i-flip diretso ang serialized asset ngadto sa
    status='maintenance' nga dili moagi sa Edit. notes optional ra, gi-
    append parehas sa gina-himo sa resolve_maintenance (timestamped, dili
    ma-overwrite). Naa say condition='for_repair' - kung status='maintenance'
    pero ang condition wala mausab (kanunay "functional" gihapon) daw
    sukwahi sa uban parts sa UI (e.g. Inventory table's Condition column),
    mao nga gi-sync ni duha, parehas sa gina-himo sa resolve_maintenance
    pag-set balik sa condition pagawas sa maintenance."""
    asset, error = get_asset_for_mark_maintenance(conn, asset_id)
    if error:
        error_payload, status, _ctx = error
        return None, (error_payload, status, {"asset": conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()})

    notes = (notes or "").strip()
    new_notes = asset["notes"]
    if notes:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{stamp}] Marked for maintenance: {notes}"
        new_notes = f"{new_notes}\n{entry}" if new_notes else entry

    try:
        conn.execute(
            "UPDATE assets SET status = 'maintenance', condition = 'for_repair', notes = ? WHERE id = ?",
            (new_notes, asset_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, ({"error": "A database constraint was violated."}, 400, {"asset": asset})

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="marked_for_maintenance",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=f"Marked {asset['asset_id']} for maintenance" + (f": {notes}" if notes else ""),
    )

    return {"message": "Asset marked for maintenance", "asset_id_code": asset["asset_id"]}, None


# # # RESOLVE MAINTENANCE (quick action, no full Edit modal needed) --------


def get_asset_for_resolve_maintenance(conn, asset_id):
    """Validation-only lookup before resolve_maintenance - kinahanglan
    naa gyud sa status='maintenance' para dinhi ma-resolve."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    if asset["status"] != "maintenance":
        return None, _no_ctx({"error": "Asset is not in maintenance"}, 400)
    return asset, None


def resolve_maintenance(conn, asset_id, condition, notes, staff_id, next_maintenance_due=None):
    """Quick action para sa "human na ang maintenance, ibalik sa
    circulation" case, dili moagi sa full Edit modal. Kanunay mo-balik
    status='available' - kung dili na maayo, disposal na na, laing action
    na sa Edit, dili dinhi. notes optional, gi-append sa existing notes
    (timestamped) dili ma-overwrite aron dili mawala ang repair history.

    checkouts_since_maintenance kanunay mag-reset to 0 diri - ang pag-
    service mo-restart sa interval schedule bisan unsa pay nag-trigger
    ana. next_maintenance_due optional, kung gi-supply mo-replace sa
    stored due date; kung blank, auto-compute ug bag-o (today + global
    default interval) dili i-clear to nothing, aron dili mawala ang
    schedule."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    if asset["status"] != "maintenance":
        return None, ({"error": "Asset is not in maintenance"}, 400, {"asset": asset})
    if condition not in ("functional", "inoperable", "for_repair"):
        return None, (
            {"error": "condition must be one of: functional, inoperable, for_repair"},
            400,
            {"asset": asset},
        )

    notes = (notes or "").strip()
    new_notes = asset["notes"]
    if notes:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{stamp}] Maintenance resolved: {notes}"
        new_notes = f"{new_notes}\n{entry}" if new_notes else entry

    if not next_maintenance_due:
        next_maintenance_due = _default_next_maintenance_due(conn, None, None)

    # Resolving as 'functional' (fixed / now good) counts as a fresh
    # calibration - anything not actually fixed (inoperable/for_repair)
    # leaves last_calibrated untouched, same as before. Date-only (not a
    # full timestamp) to match the Edit form's <input type="date"> for
    # this field and every other place it's read/displayed.
    new_last_calibrated = asset["last_calibrated"]
    if condition == "functional":
        new_last_calibrated = conn.execute("SELECT date('now', 'localtime') AS d").fetchone()["d"]

    try:
        conn.execute(
            """
            UPDATE assets
            SET status = 'available', condition = ?, notes = ?,
                checkouts_since_maintenance = 0, next_maintenance_due = ?, last_calibrated = ?
            WHERE id = ?
            """,
            (condition, new_notes, next_maintenance_due, new_last_calibrated, asset_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, ({"error": "A database constraint was violated."}, 400, {"asset": asset})

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="resolved_maintenance",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=(
            f"Resolved maintenance on {asset['asset_id']} (condition set to '{condition}')"
            + (f" - notes: {notes}" if notes else "")
        ),
    )

    return {"message": "Maintenance resolved successfully", "asset_id_code": asset["asset_id"]}, None


# # # PREVENTIVE MAINTENANCE (proactive - date or usage-interval based) ----


def get_maintenance_due_assets(conn, within_days=None):
    """Assets nga due (o hapit na due) para sa maintenance, bisan unsa nga
    trigger - date-based (next_maintenance_due within `within_days`, o
    overdue na kung within_days=None) o usage-based
    (checkouts_since_maintenance na-reach na ang maintenance_interval_
    checkouts). Wala dinhi ang naa na sa reactive maintenance flow
    (status='maintenance') ug soft-deleted assets - noise ra na tanan."""
    if within_days is not None:
        date_clause = "assets.next_maintenance_due <= date('now', 'localtime', ? || ' days')"
        date_params = (str(within_days),)
    else:
        date_clause = "assets.next_maintenance_due <= date('now', 'localtime')"
        date_params = ()

    rows = conn.execute(
        f"""
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE assets.deleted_at IS NULL
          AND assets.status != 'maintenance'
          AND (
              (assets.next_maintenance_due IS NOT NULL AND {date_clause})
              OR (
                  assets.maintenance_interval_checkouts IS NOT NULL
                  AND assets.checkouts_since_maintenance >= assets.maintenance_interval_checkouts
              )
          )
        ORDER BY assets.next_maintenance_due IS NULL, assets.next_maintenance_due, assets.name
        """,
        date_params,
    ).fetchall()
    return [dict(r) for r in rows]


# # # ADJUST STOCK (consumables only) --------------------------------------


def adjust_consumable_stock(conn, asset_id, delta, reason, staff_id):
    """Manual stock correction (restock, spoilage/loss write-off, recount)
    - lahi ni sa automatic decrement/increment sa checkout/return
    (services/transaction_service.py). `delta` signed: positive mag-dugang
    stock, negative mag-kuha. Guarded parehas sa checkout-session CAS
    pattern nga nag-guard sa assets.status - ang WHERE clause mismo ang
    nag-prevent nga dili maka-negative bisan naay concurrent adjustment,
    dili read-then-write race."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    if asset["asset_type"] != "consumable":
        return None, _no_ctx({"error": "Only consumable assets have stock to adjust"}, 400)
    if not isinstance(delta, int) or delta == 0:
        return None, _no_ctx({"error": "delta must be a non-zero whole number"}, 400)

    cursor = conn.execute(
        "UPDATE assets SET quantity = quantity + ? WHERE id = ? AND asset_type = 'consumable' AND quantity + ? >= 0",
        (delta, asset_id, delta),
    )
    if cursor.rowcount == 0:
        conn.rollback()
        return None, _no_ctx(
            {"error": f"Cannot remove {-delta} unit(s) - only {asset['quantity']} in stock"}, 409
        )
    conn.commit()

    new_quantity = conn.execute("SELECT quantity FROM assets WHERE id = ?", (asset_id,)).fetchone()["quantity"]

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="adjusted_stock",
        target_table="assets",
        target_id=asset_id,
        target_name=f"{asset['asset_id']} - {asset['name']}",
        details=(
            f"{'Added' if delta > 0 else 'Removed'} {abs(delta)} unit(s) "
            f"({asset['quantity']} -> {new_quantity})" + (f" - {reason}" if reason else "")
        ),
    )

    return {"message": "Stock adjusted successfully", "quantity": new_quantity}, None


def get_asset_by_barcode(conn, barcode):
    """Lookup by barcode_id o rfid_id (naay decimal-scan fallback para sa
    ubang RFID readers) - para sa scan-to-open flows."""
    # Some readers set to type scans as keyboard input only type the decimal
    # value of an EPC's last 3 bytes, not the full hex ID - the extra
    # `LIKE` branch below also matches on that, harmlessly a no-op (NULL)
    # when `barcode` isn't a plausible decimal scan. See
    # decimal_scan_hex_suffix()'s own docstring for the full reasoning.
    hex_suffix = decimal_scan_hex_suffix(barcode)
    asset = conn.execute(
        """
        SELECT assets.*, categories.name AS category
        FROM assets
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE (
            UPPER(assets.barcode_id) = UPPER(?)
            OR UPPER(assets.rfid_id) = UPPER(?)
            OR UPPER(assets.rfid_id) LIKE '%' || ?
        ) AND assets.deleted_at IS NULL
        """,
        (barcode, barcode, hex_suffix),
    ).fetchone()

    if asset is None:
        return None, _no_ctx({"error": "Asset not found"}, 404)
    return dict(asset), None


def fetch_asset_history(asset_id, limit, offset, conn):
    """Gina-share sa JSON history endpoint ug sa history modal, aron
    parehas gyud silang duha, dili mag-drift sa usag-usa."""
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
    """Paginated transaction history sa asset, JSON endpoint version
    (plain dict lang, walay asset row)."""
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
    """Parehas ra sa get_asset_history, pero naay full asset row -
    gikinahanglan sa History modal template para sa header info."""
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
    """Exact-match lookup para sa Enter key sa search box (physical
    barcode scan) - mag-try sa barcode_id una, fallback sa rfid_id, aron
    barcode OR RFID tag ID nga na-type/scan diretso mo-jump sa same asset.
    Naay fallback pud sa decimal_scan_hex_suffix() para sa readers nga
    mo-type sa decimal value ra sa EPC's last 3 bytes, dili full hex ID.
    Parehas identifier-agnostic match sa monitoring_service.py's
    record_rfid_event."""
    row = conn.execute("SELECT id FROM assets WHERE UPPER(barcode_id) = UPPER(?)", (barcode,)).fetchone()
    if row:
        return row["id"]
    row = conn.execute("SELECT id FROM assets WHERE UPPER(rfid_id) = UPPER(?)", (barcode,)).fetchone()
    if row:
        return row["id"]
    hex_suffix = decimal_scan_hex_suffix(barcode)
    if hex_suffix is None:
        return None
    row = conn.execute("SELECT id FROM assets WHERE UPPER(rfid_id) LIKE '%' || ?", (hex_suffix,)).fetchone()
    return row["id"] if row else None


def get_asset_details(conn, asset_id):
    """Read-only 'Details' modal snapshot - naa say current borrower/due
    date nga wala sa ubang modals."""
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
