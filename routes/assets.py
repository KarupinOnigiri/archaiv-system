from flask import (
    Blueprint,
    jsonify,
    g,
    request,
    render_template,
    make_response,
    current_app,
)
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from services.settings_service import is_feature_enabled, get_feature_label
from services import asset_service
from utils.logger import log_action
from utils.validators import validate_date_fields, parse_pagination_params
from utils.uploads import validate_image_upload
from utils.exports import sanitize_export_filename, default_export_dir, resolve_export_dir
from extensions import limiter
from werkzeug.security import check_password_hash
import json
import os

assets_bp = Blueprint("assets", __name__)


@assets_bp.before_request
def _block_if_assets_disabled():
    """Blocks this blueprint when Inventory is disabled; returns JSON 403, not a redirect."""
    if not is_feature_enabled("assets"):
        return jsonify({"error": f"{get_feature_label('assets')} is currently disabled"}), 403


ADDITIONAL_IMAGE_SLOTS = asset_service.ADDITIONAL_IMAGE_SLOTS


# # # LIST / VIEW ------------------------------------------------------------


def get_assets_table_context(args):
    """Opens its own connection; shared by view_assets() here and assets_page() in routes/pages.py."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    conn = get_db_connection()
    try:
        return asset_service.get_assets_table_context(conn, args, limit, offset)
    finally:
        conn.close()


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
    """The 4 stat-card numbers, refetched on 'assets-refreshed' to stay in sync after delete/import."""
    conn = get_db_connection()
    try:
        stats = asset_service.get_asset_stat_cards(conn)
    finally:
        conn.close()
    return render_template("assets/_stat_cards.html", **stats)


# # # CATEGORIES (lookup table) -------------------------------------------


@assets_bp.route("/categories")
@login_required
def view_categories():
    conn = get_db_connection()
    try:
        categories = asset_service.list_categories(conn)
    finally:
        conn.close()
    return jsonify([dict(row) for row in categories])


@assets_bp.route("/categories/manage-form")
@roles_required("admin", "in_charge")
def manage_categories_form():
    conn = get_db_connection()
    try:
        categories = asset_service.get_categories_with_asset_count(conn)
    finally:
        conn.close()
    return render_template("assets/_categories_modal.html", categories=categories)


@assets_bp.route("/categories/options")
@login_required
def category_options():
    """<option> tags for the category filter dropdown, in custom order.
    Refetched on the modal's `categories-changed` event; mirrors department_options()."""
    conn = get_db_connection()
    try:
        categories = asset_service.list_categories(conn)
    finally:
        conn.close()
    return render_template("assets/_category_options.html", categories=categories)


@assets_bp.route("/categories/add", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def add_category():
    data, error = get_json_or_error()
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = asset_service.add_category(conn, data.get("name"), g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify({"message": "Category added successfully", **result}), 201


@assets_bp.route("/categories/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def edit_category(id):
    data, error = get_json_or_error()
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = asset_service.edit_category(conn, id, data.get("name"), g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@assets_bp.route("/categories/reorder", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def reorder_categories():
    """Saves category order from the Manage Categories modal. Mirrors reorder_departments."""
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

    conn = get_db_connection()
    try:
        result, error = asset_service.reorder_categories(conn, ordered_ids, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@assets_bp.route("/categories/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def delete_category(id):
    """Blocks deleting a category with active assets; asks for reassignment first."""
    conn = get_db_connection()
    try:
        result, error = asset_service.delete_category(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


# # # CREATE ------------------------------------------------------------


@assets_bp.route("/assets/add-form")
@roles_required("admin", "in_charge")
def add_asset_form():
    conn = get_db_connection()
    try:
        categories = asset_service.list_categories(conn)
    finally:
        conn.close()
    return render_template("assets/_add_modal.html", categories=categories, error=None)


def _add_asset_form_error(error_msg):
    conn = get_db_connection()
    try:
        categories = asset_service.list_categories(conn)
    finally:
        conn.close()
    return render_template("assets/_add_modal.html", categories=categories, error=error_msg), 200


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

    # barcode_id isn't from the form -- auto-generated to match asset_id, so it's always uniform.
    required_fields = ["name"]
    for field in required_fields:
        if field not in data or not data[field]:
            error_msg = f"Missing required field: {field}"
            if request.is_json:
                return jsonify({"error": error_msg}), 400
            return _add_asset_form_error(error_msg)

    error = validate_date_fields(data, ("acquired_date", "last_calibrated"))
    if error:
        return error

    # Image is optional; validate before touching the DB to avoid a half-created asset.
    image_file = request.files.get("image") if request.files else None
    has_image = image_file is not None and image_file.filename != ""

    if has_image:
        img_error = validate_image_upload(image_file)
        if img_error:
            if request.is_json:
                return jsonify({"error": img_error}), 400
            return _add_asset_form_error(img_error)

    # Same reasoning: validate additional images up front too, before any DB writes.
    additional_image_files = {}
    for slot in ADDITIONAL_IMAGE_SLOTS:
        file = request.files.get(f"additional_image_{slot}") if request.files else None
        if file is not None and file.filename != "":
            img_error = validate_image_upload(file)
            if img_error:
                if request.is_json:
                    return jsonify({"error": img_error}), 400
                return _add_asset_form_error(img_error)
            additional_image_files[slot] = file

    conn = get_db_connection()
    try:
        result, error = asset_service.create_asset(
            conn, data, image_file if has_image else None, additional_image_files, g.staff["id"]
        )
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        error_msg = error_payload.get("error")
        if request.is_json:
            return jsonify(error_payload), status
        return _add_asset_form_error(error_msg)

    if request.is_json:
        return (
            jsonify({"message": "Asset added successfully", "id": result["id"], "asset_id": result["asset_id"]}),
            201,
        )
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/assets"
    return response


# # # BULK IMPORT (CSV) ------------------------------------------------------


@assets_bp.route("/assets/import-form")
@roles_required("admin", "in_charge")
def import_assets_form():
    return render_template("assets/_import_modal.html", error=None)


def _import_error_response(error_msg, status):
    """JSON for API callers, else re-renders the modal with an inline error via HX-Retarget."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    response = make_response(render_template("assets/_import_modal.html", error=error_msg), 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


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

    try:
        content = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return _import_error_response("File must be UTF-8 encoded CSV", 400)

    result, error = asset_service.import_assets_csv(content, mode, g.staff["id"])

    if error:
        error_payload, status, _ctx = error
        return _import_error_response(error_payload.get("error"), status)

    if result["dry_run"]:
        return render_template(
            "assets/_import_preview.html",
            rows_to_create=result["rows_to_create"],
            rows_to_archive=result["rows_to_archive"],
            errors=result["errors"],
            new_categories=result["new_categories"],
        )

    if request.is_json:
        return (
            jsonify({"created_count": result["created_count"], "created": result["created"], "errors": result["errors"]}),
            201 if result["created"] else 400,
        )

    response = make_response(
        render_template(
            "assets/_import_results.html",
            created_count=result["created_count"],
            created=result["created"],
            created_ids=result["created_ids"],
            errors=result["errors"],
            archived_count=result["archived_count"],
        )
    )
    if result["created"]:
        response.headers["HX-Trigger"] = "assets-refreshed"
    return response


# # # EXPORT --------------------------------------------------------------


@assets_bp.route("/assets/export/browse")
@login_required
def browse_export_folders():
    """Lists subfolders under the app root for the export folder picker; clamped like the save-path."""
    app_root = os.path.realpath(current_app.root_path)
    try:
        result = asset_service.list_export_subfolders(app_root, request.args.get("path", ""))
    except OSError as e:
        return jsonify({"error": f"Could not list folder: {e}"}), 400
    return jsonify(result)


@assets_bp.route("/assets/export/browse/mkdir", methods=["POST"])
@login_required
def create_export_folder():
    """Creates a subfolder from the folder picker. Bad names are rejected outright, not sanitized."""
    app_root = os.path.realpath(current_app.root_path)
    try:
        rel_path, error_msg = asset_service.create_export_subfolder(
            app_root, request.form.get("path", ""), request.form.get("name")
        )
    except OSError as e:
        return jsonify({"error": f"Could not create folder: {e}"}), 400

    if error_msg:
        return jsonify({"error": error_msg}), 400
    return jsonify({"path": rel_path})


@assets_bp.route("/assets/export-form")
@login_required
def export_assets_form():
    """Opens the export modal; show_archived matches whichever table view the user was on."""
    show_archived = request.args.get("show_archived") == "1"
    fields = list(asset_service.EXPORT_CSV_COLUMNS)
    if show_archived:
        fields.append("deleted_at")
    return render_template(
        "assets/_export_modal.html",
        show_archived=show_archived,
        default_filename=asset_service.default_export_basename(show_archived),
        default_export_dir=default_export_dir(),
        fields=[(col, asset_service.EXPORT_FIELD_LABELS[col]) for col in fields],
    )


@assets_bp.route("/assets/export", methods=["GET", "POST"])
@login_required
def export_assets():
    """GET exports CSV/Excel read-only; POST also saves a copy server-side, gated for CSRF on the disk write."""
    show_archived = request.values.get("show_archived") == "1"
    export_format = request.values.get("format", "csv").lower()
    if export_format not in ("csv", "xlsx"):
        export_format = "csv"

    columns = asset_service.resolve_export_columns(show_archived, request.values.getlist("fields"))

    conn = get_db_connection()
    try:
        assets = asset_service.get_assets_for_export(conn, show_archived)
    finally:
        conn.close()

    basename = sanitize_export_filename(
        request.values.get("filename"), asset_service.default_export_basename(show_archived)
    )

    file_bytes, content_type, export_format = asset_service.build_assets_export_file(
        assets, columns, export_format, show_archived
    )
    filename = f"{basename}.{export_format}"

    response = make_response(file_bytes)
    response.headers["Content-Type"] = content_type
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"

    if request.method == "POST":
        save_dir, was_clamped = resolve_export_dir(request.values.get("save_path"))
        saved_path, warning = asset_service.save_export_copy(file_bytes, save_dir, filename)
        if saved_path:
            response.headers["X-Export-Saved-Path"] = saved_path
            if was_clamped:
                response.headers["X-Export-Warning"] = (
                    "The folder entered was outside the app directory, "
                    "so the export was saved to the default export folder instead."
                )
        elif warning:
            response.headers["X-Export-Warning"] = warning

    return response


# # # SINGLE ASSET (view/edit/delete/restore) ------------------------------


@assets_bp.route("/assets/<int:id>")
@login_required
def get_asset(id):
    conn = get_db_connection()
    try:
        asset, error = asset_service.get_asset(conn, id)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(asset)


@assets_bp.route("/assets/<int:id>/edit-form")
@roles_required("admin", "in_charge")
def edit_asset_form(id):
    conn = get_db_connection()
    try:
        result, error = asset_service.get_asset_for_edit(conn, id)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return_to = request.args.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"
    return render_template(
        "assets/_edit_modal.html",
        asset=result["asset"],
        categories=result["categories"],
        additional_images=result["additional_images"],
        error=None,
        return_to=return_to,
    )


def _edit_error_response(error_payload, status, asset=None, asset_id=None, return_to="table"):
    """Returns JSON if this was a JSON request; renders the edit modal
    with an inline error if it was an htmx/form request."""
    if request.is_json:
        return jsonify(error_payload), status
    conn = get_db_connection()
    try:
        if asset is None:
            asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        categories = asset_service.list_categories(conn)
        additional_images = asset_service.fetch_asset_images(asset["id"], conn)
    finally:
        conn.close()
    error_msg = error_payload.get("error") if isinstance(error_payload, dict) else error_payload
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

    # From the edit form's hidden field, so Cancel still routes right after a failed re-render.
    return_to = data.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"

    error = validate_date_fields(data, ("acquired_date", "last_calibrated"))
    if error:
        return error

    condition = data.get("condition")
    if condition is not None and condition not in asset_service.VALID_CONDITIONS:
        return _edit_error_response(
            {"error": f"condition must be one of: {', '.join(asset_service.VALID_CONDITIONS)}"},
            400,
            asset_id=id,
            return_to=return_to,
        )

    image_file = request.files.get("image") if request.files else None
    has_new_image = image_file is not None and image_file.filename != ""
    remove_image = str(data.get("remove_image", "0")) == "1"

    additional_image_updates = {}
    for slot in ADDITIONAL_IMAGE_SLOTS:
        file = request.files.get(f"additional_image_{slot}") if request.files else None
        has_new = file is not None and file.filename != ""
        remove_flag = str(data.get(f"remove_additional_image_{slot}", "0")) == "1"
        additional_image_updates[slot] = {"file": file if has_new else None, "remove": remove_flag}

    swap_requested = str(data.get("swap_with_slot", "")).strip()

    conn = get_db_connection()
    try:
        result, error = asset_service.update_asset(
            conn,
            id,
            data,
            image_file if has_new_image else None,
            remove_image,
            additional_image_updates,
            swap_requested,
            g.staff["id"],
        )
    finally:
        conn.close()

    if error:
        error_payload, status, ctx = error
        if "asset" not in ctx:
            return jsonify(error_payload), status
        return _edit_error_response(error_payload, status, ctx["asset"], return_to=return_to)

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/assets"
    return response


@assets_bp.route("/assets/<int:id>/delete-confirm")
@roles_required("admin", "in_charge")
def delete_asset_confirm(id):
    conn = get_db_connection()
    try:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
    finally:
        conn.close()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    return_to = request.args.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"
    return render_template("assets/_delete_modal.html", asset=asset, error=None, return_to=return_to)


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
    try:
        result, error = asset_service.delete_asset(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, ctx = error
        if "asset" not in ctx:
            return jsonify(error_payload), status
        if request.is_json:
            return jsonify(error_payload), status
        error_msg = ctx.get("plain_message", error_payload.get("error"))
        return render_template("assets/_delete_modal.html", asset=ctx["asset"], error=error_msg, return_to=return_to), 200

    if request.is_json:
        return jsonify({"message": result["message"]})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "assets-refreshed": True,
            "show-toast": {
                "message": f"{result['asset_id_code']} deleted successfully",
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
    """Undo button on the delete toast; admin/in_charge like delete, no condition prompt needed."""
    conn = get_db_connection()
    try:
        result, error = asset_service.undo_delete_asset(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@assets_bp.route("/assets/bulk-delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("10 per minute")
def bulk_delete_assets():
    """Soft-deletes multiple assets; a bad row goes into `skipped` instead of failing the whole batch."""
    data, error = get_json_or_error()
    if error:
        return error

    raw_ids = data.get("ids")
    if not raw_ids or not isinstance(raw_ids, list):
        return jsonify({"error": "Missing required field: ids (must be a non-empty list)"}), 400

    try:
        ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify({"error": "ids must be a list of integers"}), 400

    conn = get_db_connection()
    try:
        result, _error = asset_service.bulk_delete_assets(conn, ids, g.staff["id"])
    finally:
        conn.close()

    return jsonify(result)


@assets_bp.route("/assets/bulk-restore", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def bulk_restore_assets():
    """Restores multiple archived assets, admin-only. Disposed ones are skipped -- no per-row condition UI."""
    data, error = get_json_or_error()
    if error:
        return error

    raw_ids = data.get("ids")
    if not raw_ids or not isinstance(raw_ids, list):
        return jsonify({"error": "Missing required field: ids (must be a non-empty list)"}), 400

    try:
        ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify({"error": "ids must be a list of integers"}), 400

    conn = get_db_connection()
    try:
        result, _error = asset_service.bulk_restore_assets(conn, ids, g.staff["id"])
    finally:
        conn.close()

    return jsonify(result)


@assets_bp.route("/assets/<int:id>/restore-confirm")
@roles_required("admin")
def restore_asset_confirm(id):
    conn = get_db_connection()
    try:
        asset, error = asset_service.get_asset_for_restore(conn, id)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return_to = request.args.get("return_to", "table")
    if return_to not in ("table", "details"):
        return_to = "table"
    return render_template("assets/_restore_modal.html", asset=asset, error=None, return_to=return_to)


def _restore_error_response(error_msg, status, asset_id, return_to="table"):
    """Mirrors _edit_error_response: JSON for API callers, a re-rendered
    modal with the error inline for htmx/form callers."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    conn = get_db_connection()
    try:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    finally:
        conn.close()
    if return_to not in ("table", "details"):
        return_to = "table"
    return render_template("assets/_restore_modal.html", asset=asset, error=error_msg, return_to=return_to), 200


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

    # Restore needs the admin's own password plus a logged reason, same as reactivate_staff.
    current_password = (data.get("current_password") or "").strip()
    if not current_password:
        return _restore_error_response("Enter your password to confirm this change", 400, id, return_to=return_to)
    if not check_password_hash(g.staff["password_hash"], current_password):
        return _restore_error_response("Current password is incorrect", 401, id, return_to=return_to)
    reason = (data.get("reason") or "").strip()
    if not reason:
        return _restore_error_response("A reason is required to restore this asset", 400, id, return_to=return_to)

    if condition is not None and condition not in asset_service.VALID_CONDITIONS:
        return _restore_error_response(
            f"condition must be one of: {', '.join(asset_service.VALID_CONDITIONS)}", 400, id, return_to=return_to
        )
    if condition == "disposed":
        return _restore_error_response(
            "Cannot restore an asset with condition 'disposed' -- choose a different condition",
            400,
            id,
            return_to=return_to,
        )

    conn = get_db_connection()
    try:
        result, error = asset_service.restore_asset(conn, id, condition, reason, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, ctx = error
        if "asset_id" not in ctx:
            return jsonify(error_payload), status
        return _restore_error_response(error_payload.get("error"), status, ctx["asset_id"], return_to=return_to)

    if request.is_json:
        return jsonify({"message": result["message"]})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "assets-refreshed": True,
            "show-toast": {
                "message": f"{result['asset_id_code']} restored successfully",
                "type": "success",
                "action": {
                    "label": "Undo",
                    # Re-archiving undoes a restore; delete's looser role check still covers it.
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
    try:
        asset, error = asset_service.get_asset_by_barcode(conn, barcode)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(asset)


@assets_bp.route("/assets/<int:id>/history")
@login_required
def get_asset_history(id):
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = asset_service.get_asset_history(conn, id, limit, offset)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@assets_bp.route("/assets/<int:id>/history-modal")
@login_required
def asset_history_modal(id):
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = asset_service.get_asset_history_modal_data(conn, id, limit, offset)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return render_template(
        "assets/_history_modal.html",
        asset=result["asset"],
        history=result["history"],
        total_count=result["total_count"],
        limit=result["limit"],
        offset=result["offset"],
    )


# # # DETAILS (read-only, opened by clicking a row) --------------------------


@assets_bp.route("/assets/by-barcode/<string:barcode>")
@login_required
def asset_by_barcode(barcode):
    """Exact-match barcode lookup for the search box's Enter key; returns 200 even on a miss for htmx."""
    conn = get_db_connection()
    try:
        asset_id = asset_service.find_asset_id_by_barcode(conn, barcode)
    finally:
        conn.close()
    if asset_id is None:
        return render_template("_barcode_not_found_modal.html", kind="inventory item", barcode=barcode)
    return asset_details_modal(asset_id)


@assets_bp.route("/assets/<int:id>/details-modal")
@login_required
def asset_details_modal(id):
    """Read-only 'Details' modal snapshot; adds current borrower/due date that the other modals skip."""
    conn = get_db_connection()
    try:
        result, error = asset_service.get_asset_details(conn, id)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return render_template(
        "assets/_details_modal.html",
        asset=result["asset"],
        current_loan=result["current_loan"],
        recent_history=result["recent_history"],
        history_total_count=result["history_total_count"],
        additional_images=result["additional_images"],
    )
