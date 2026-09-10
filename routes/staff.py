from flask import Blueprint, jsonify, g, request, render_template, make_response, url_for, send_file
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.uploads import validate_image_upload, save_staff_photo
from services.settings_service import (
    get_session_idle_minutes,
    get_session_absolute_hours,
    get_max_failed_attempts,
    get_lockout_minutes,
    get_due_soon_hours,
    FEATURE_FLAGS,
    get_feature_flags,
    get_default_loan_days,
    get_damage_restriction_threshold,
    get_archive_purge_days,
    get_checkout_session_stale_minutes,
    get_default_maintenance_interval_days,
    get_maintenance_reminder_lead_days,
    get_alarm_enabled,
    get_asset_id_prefix,
    get_member_code_prefix,
    get_staff_code_prefix,
)
from services import staff_service
from utils.validators import (
    validate_date_fields,
    validate_password_strength,
    validate_rfid_pin,
    parse_pagination_params,
)
from werkzeug.security import check_password_hash
from utils.exports import sanitize_export_filename, default_export_dir, resolve_export_dir
from extensions import limiter

"""Thin HTTP layer para sa /staff/*, /audit-log, /admin-deactivation-requests
ug system-settings endpoints - validation ug business logic naa sa
services/staff_service.py."""

staff_bp = Blueprint("staff", __name__)
VALID_ROLES = staff_service.VALID_ROLES
MAX_MANUAL_LOCK_MINUTES = staff_service.MAX_MANUAL_LOCK_MINUTES


# # # LIST / VIEW ------------------------------------------------------------


def get_staff_table_context(args):
    """Mag-open ug kaugalingong connection; gi-share ni sa view_staff() diri ug sa staff_page() sa routes/pages.py."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    conn = get_db_connection()
    try:
        return staff_service.get_staff_table_context(conn, args, limit, offset)
    finally:
        conn.close()


@staff_bp.route("/staff")
@roles_required("admin")
def view_staff():
    """GET - main Staff table, htmx partial or JSON depending sa HX-Request header."""
    context, error = get_staff_table_context(request.args)
    if error:
        return error

    if request.headers.get("HX-Request") == "true":
        # staff=g.staff needed here too - _table.html's can_manage check reads it on every htmx swap.
        return render_template("staff/_table.html", staff=g.staff, **context)

    return jsonify(
        {
            "total_count": context["total_count"],
            "limit": context["limit"],
            "offset": context["offset"],
            "results": [dict(row) for row in context["staff_list"]],
        }
    )


@staff_bp.route("/staff/<int:id>")
@login_required
def get_staff_by_id(id):
    """GET - single staff record as JSON. Delegates to staff_service.get_staff_by_id()."""
    conn = get_db_connection()
    try:
        staff, error = staff_service.get_staff_by_id(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(staff)


@staff_bp.route("/staff/by-barcode/<string:barcode>")
@login_required
def staff_by_barcode(barcode):
    """GET - exact-match barcode lookup para sa search box's Enter key, via the linked member's barcode."""
    conn = get_db_connection()
    try:
        staff_id = staff_service.find_staff_id_by_barcode(conn, barcode)
    finally:
        conn.close()
    if staff_id is None:
        return render_template("_barcode_not_found_modal.html", kind="staff member", barcode=barcode)
    return staff_profile(staff_id)


@staff_bp.route("/staff/<int:id>/profile")
@login_required
def staff_profile(id):
    """GET - read-only profile modal; ang Activity tab admin-only ra kay ang audit-log naga-filter by staff_id sa admins ra."""
    conn = get_db_connection()
    try:
        result, error = staff_service.get_staff_profile(conn, id)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return render_template(
        "staff/_profile_modal.html",
        staff_member=result["staff_member"],
        pending_deactivation_request=result["pending_deactivation_request"],
        usage_status=result["usage_status"],
        usage_days_left=result["usage_days_left"],
    )


@staff_bp.route("/staff/<int:id>/full-activity-modal")
@roles_required("admin")
def staff_full_activity_modal(id):
    """GET - paginated "Full Activity" popup nga naga-stack sa profile modal, admin-only ra pareho sa tab."""
    conn = get_db_connection()
    try:
        staff_member, error = staff_service.get_staff_for_full_activity(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("staff/_full_activity_modal.html", staff_member=staff_member)


def get_audit_log_context(args, staff):
    """Mag-open ug kaugalingong connection; gi-share sa view_audit_log() ug view_audit_log_table()."""
    conn = get_db_connection()
    try:
        return staff_service.get_audit_log_context(conn, args, staff)
    finally:
        conn.close()


@staff_bp.route("/audit-log")
@login_required
def view_audit_log():
    """GET - audit log entries as JSON. Delegates to get_audit_log_context() sa taas."""
    context, error = get_audit_log_context(request.args, g.staff)
    if error:
        return error

    return jsonify(
        {
            "total_count": context["total_count"],
            "limit": context["limit"],
            "offset": context["offset"],
            "results": [dict(row) for row in context["logs"]],
        }
    )


@staff_bp.route("/audit-log/table")
@login_required
def view_audit_log_table():
    """GET - htmx partial, table + pagination footer ra, para dili na
    kinahanglan i-re-render ang whole page every filter/page."""
    context, error = get_audit_log_context(request.args, g.staff)
    if error:
        return error
    return render_template("audit_log/_table.html", **context)


# # # CREATE (admin only) ------------------------------------------------


@staff_bp.route("/staff/add-form")
@roles_required("admin")
def add_staff_form():
    """GET - opens ang "Add New Staff" modal; naga-grant access sa existing member ra, mao naay type-ahead picker imbes dropdown."""
    return render_template("staff/_add_modal.html")


@staff_bp.route("/staff/eligible-members-search")
@roles_required("admin")
def eligible_members_search():
    """GET - Add Staff's member picker; naga-match sa name/code/barcode para sa active non-staff members, capped ang results."""
    BROWSE_PAGE_SIZE = 50

    q = request.args.get("q", "").strip()
    browse_all = request.args.get("browse") == "1"
    append = request.args.get("append") == "1"

    if not q and not browse_all:
        return render_template(
            "staff/_eligible_member_results.html", results=[], q=q, browse_all=False, has_more=False,
            loaded_so_far=0, next_offset=0,
        )

    offset = 0
    if browse_all:
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0

    conn = get_db_connection()
    try:
        result = staff_service.search_eligible_members(conn, q, browse_all, offset, BROWSE_PAGE_SIZE)
    finally:
        conn.close()

    if browse_all:
        template = "staff/_eligible_member_rows_append.html" if append else "staff/_eligible_member_results.html"
        return render_template(
            template, results=result["results"], q=q, browse_all=True, has_more=result["has_more"],
            loaded_so_far=result["loaded_so_far"], next_offset=result["next_offset"],
        )

    return render_template(
        "staff/_eligible_member_results.html", results=result["results"], q=q, browse_all=False,
        has_more=False, loaded_so_far=result["loaded_so_far"], next_offset=0,
    )


@staff_bp.route("/staff/add", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def add_staff_submit():
    """POST - mag-create ug new staff account gikan sa existing member. Delegates sa staff_service.create_staff()."""
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = ["username", "password", "role", "member_id"]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    if data["role"] not in VALID_ROLES:
        return jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400

    error = validate_date_fields(data, ("assigned_start", "assigned_end"))
    if error:
        return error

    error = validate_password_strength(data["password"])
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = staff_service.create_staff(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return jsonify({"message": "Staff added successfully", **result}), 201


# # # UPDATE ------------------------------------------------------------


@staff_bp.route("/staff/<int:id>/edit-form")
@roles_required("admin")
def edit_staff_form(id):
    """GET - opens ang "Edit Staff" modal; role ug assigned_start/end ra ang editable, ang uban read-only context ra."""
    conn = get_db_connection()
    try:
        staff_member, error = staff_service.get_staff_for_edit(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("staff/_edit_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/reassign-form")
@roles_required("admin")
def reassign_usage_form(id):
    """GET - quick "Reassign Usage" modal para i-extend/shorten ang assignment window; nag-post ra ni sa same edit endpoint."""
    conn = get_db_connection()
    try:
        staff_member, error = staff_service.get_staff_for_reassign(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("staff/_reassign_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/edit", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def edit_staff_submit(id):
    """POST - i-update ang role/assignment window/rfid_pin sa staff. Delegates sa staff_service.update_staff()."""
    data, error = get_json_or_error()
    if error:
        return error

    if "role" in data and data["role"] not in VALID_ROLES:
        return jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400

    error = validate_date_fields(data, ("assigned_start", "assigned_end"))
    if error:
        return error

    # A blank rfid_pin leaves the existing one untouched - not wiped by an empty submit.
    if data.get("rfid_pin"):
        error = validate_rfid_pin(data["rfid_pin"])
        if error:
            return error

    conn = get_db_connection()
    try:
        result, error = staff_service.update_staff(conn, id, data, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


# # # PASSWORD MANAGEMENT ------------------------------------------------


@staff_bp.route("/staff/change-password", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def change_password_submit():
    """POST - self-service password change; kinahanglan ang current password para ma-prove nga dili ni hijacked session."""
    data, error = get_json_or_error()
    if error:
        return error

    current_password = data.get("current_password")
    new_password = data.get("new_password")

    if not current_password or not new_password:
        return jsonify({"error": "current_password and new_password are required"}), 400

    if not check_password_hash(g.staff["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    error = validate_password_strength(new_password, "new_password")
    if error:
        return error

    current_token = request.cookies.get("session_token")

    conn = get_db_connection()
    try:
        result = staff_service.change_own_password(conn, g.staff, new_password, current_token)
    finally:
        conn.close()

    return jsonify(result)


# # # BARCODE + PIN LOGIN (self-service, any staff role) --------------------
#
# The old admin-only "change-rfid-pin" route that used to sit above this
# section (for the now fully-removed RFID+PIN login flow) was deleted along
# with it - see login_service.py's module docstring for the full story on
# staff.rfid_pin_hash's naming.


@staff_bp.route("/staff/barcode-pin/enable", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def enable_barcode_pin_login_submit():
    """POST - i-turn on ang barcode + PIN login para sa calling staff ug i-set
    ang PIN one step ra - open ni sa tanan role (tan-awa
    0051_add_staff_barcode_pin_enabled.sql: general opt-in
    toggle ni, dili ang admin-only RFID+PIN flow nga gi-replace ni sa login)."""
    data, error = get_json_or_error()
    if error:
        return error

    current_password = data.get("current_password")
    new_pin = data.get("new_pin")

    if not current_password or not new_pin:
        return jsonify({"error": "current_password and new_pin are required"}), 400

    if not check_password_hash(g.staff["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    error = validate_rfid_pin(new_pin)
    if error:
        return error

    current_token = request.cookies.get("session_token")

    conn = get_db_connection()
    try:
        result = staff_service.enable_barcode_pin_login(conn, g.staff, new_pin, current_token)
    finally:
        conn.close()

    return jsonify(result)


@staff_bp.route("/staff/barcode-pin/disable", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def disable_barcode_pin_login_submit():
    """POST - i-turn off ang barcode + PIN login. Naga-confirm with the
    password same sa uban credential changes diri, bisan kung *remove* ra ni
    ug way in, dili mag-add - consistent > clever, ug para dili ma-abuse
    kung naay laing tao naka-unlocked pa'ng session sa staff."""
    data, error = get_json_or_error()
    if error:
        return error

    current_password = data.get("current_password")
    if not current_password:
        return jsonify({"error": "current_password is required"}), 400

    if not check_password_hash(g.staff["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    conn = get_db_connection()
    try:
        result = staff_service.disable_barcode_pin_login(conn, g.staff)
    finally:
        conn.close()

    return jsonify(result)


@staff_bp.route("/staff/change-barcode-pin", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def change_barcode_pin_submit():
    """POST - self-service PIN change kung naka-enable na ang barcode + PIN
    login - same shape ra sa change-rfid-pin sa taas, open lang sa tanan role."""
    data, error = get_json_or_error()
    if error:
        return error

    current_password = data.get("current_password")
    new_pin = data.get("new_pin")

    if not current_password or not new_pin:
        return jsonify({"error": "current_password and new_pin are required"}), 400

    if not check_password_hash(g.staff["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    if not g.staff["barcode_pin_enabled"]:
        return jsonify({"error": "Barcode + PIN login isn't enabled for this account"}), 400

    error = validate_rfid_pin(new_pin)
    if error:
        return error

    current_token = request.cookies.get("session_token")

    conn = get_db_connection()
    try:
        result = staff_service.change_own_barcode_pin(conn, g.staff, new_pin, current_token)
    finally:
        conn.close()

    return jsonify(result)


# # # NOTIFICATION PREFERENCES --------------------------------------------


@staff_bp.route("/staff/notification-prefs", methods=["POST"])
@login_required
def update_own_notification_prefs():
    """POST - naga-handle sa notify_* toggles ug toast duration; personal prefs ni, mao self-service ra, dili admin task."""
    data, error = get_json_or_error()
    if error:
        return error

    updates, error_msg = staff_service.parse_notification_pref_updates(data)
    if error_msg:
        return jsonify({"error": error_msg}), 400

    conn = get_db_connection()
    try:
        result = staff_service.save_own_notification_prefs(conn, updates, g.staff)
    finally:
        conn.close()

    return jsonify(result)


# # # PROFILE PHOTO -----------------------------------------------------


@staff_bp.route("/staff/photo", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def upload_own_photo():
    """POST - self-service photo upload; i-delete ang old file after commit ra, para dili ma-orphan kung mag-fail."""
    photo_file = request.files.get("photo") if request.files else None
    img_error = validate_image_upload(photo_file)
    if img_error:
        return jsonify({"error": img_error}), 400

    saved_path, img_error = save_staff_photo(photo_file, g.staff["username"])
    if img_error:
        return jsonify({"error": img_error}), 400

    conn = get_db_connection()
    try:
        result = staff_service.upload_own_photo(conn, g.staff, saved_path)
    finally:
        conn.close()

    result["photo_url"] = url_for("static", filename=saved_path)
    return jsonify(result)


@staff_bp.route("/staff/photo", methods=["DELETE"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def remove_own_photo():
    """DELETE - self-service photo removal, mo-revert sa initials; no-op ra (still 200) kung wala man gyu'y photo."""
    conn = get_db_connection()
    try:
        result = staff_service.remove_own_photo(conn, g.staff)
    finally:
        conn.close()
    return jsonify(result)


@staff_bp.route("/staff/<int:id>/reset-password-form")
@roles_required("admin")
def reset_password_form(id):
    """GET - confirm-before-reset modal. Mirrors lock_staff_form's shape; ang
    actual reset naa sa reset_password_submit sa ubos."""
    conn = get_db_connection()
    try:
        staff_member, error = staff_service.get_staff_for_reset_password(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("staff/_reset_password_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/reset-password", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute; 30 per hour")
def reset_password_submit(id):
    """POST - admin-initiated reset; mag-generate ug random password (shown once ra) ug mag-kill sa existing sessions."""
    conn = get_db_connection()
    try:
        result, error = staff_service.reset_staff_password(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@staff_bp.route("/staff/<int:id>/lock-form")
@roles_required("admin")
def lock_staff_form(id):
    """GET - manual-lock modal - naay duration presets, custom minutes field,
    ug indefinite option. Mirrors delete_staff_form's shape."""
    conn = get_db_connection()
    try:
        staff_member, error = staff_service.get_staff_for_lock(conn, id, g.staff["id"])
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("staff/_lock_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/lock", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def lock_staff(id):
    """POST - manual lock sa account, naga-write sa same locked_until column nga gigamit sa auto-lockout."""
    data, error = get_json_or_error()
    if error:
        return error

    if id == g.staff["id"]:
        return jsonify({"error": "You can't lock your own account"}), 403

    indefinite = bool(data.get("indefinite"))
    duration_minutes = data.get("duration_minutes")

    if not indefinite:
        try:
            duration_minutes = int(duration_minutes)
        except (TypeError, ValueError):
            return jsonify({"error": "duration_minutes must be a whole number of minutes"}), 400
        if duration_minutes <= 0:
            return jsonify({"error": "duration_minutes must be greater than 0"}), 400
        if duration_minutes > MAX_MANUAL_LOCK_MINUTES:
            return (
                jsonify(
                    {"error": f"duration_minutes can't exceed {MAX_MANUAL_LOCK_MINUTES} (1 year) - use an indefinite lock instead"}
                ),
                400,
            )

    conn = get_db_connection()
    try:
        result, error = staff_service.lock_staff(conn, id, g.staff["id"], indefinite, duration_minutes)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@staff_bp.route("/staff/<int:id>/unlock-form")
@roles_required("admin")
def unlock_staff_form(id):
    """GET - confirm-before-unlock modal, puli sa plain browser confirm(); mirrors lock_staff_form."""
    conn = get_db_connection()
    try:
        staff_member, error = staff_service.get_staff_for_unlock(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("staff/_unlock_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/unlock", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def unlock_staff(id):
    """POST - manual clear sa lockout before its time; walay self-service recovery kay offline deployment ni."""
    conn = get_db_connection()
    try:
        result, error = staff_service.unlock_staff(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


# # # DELETE (deactivate, admin only) ------------------------------------


@staff_bp.route("/staff/<int:id>/delete-confirm")
@roles_required("admin")
def delete_staff_form(id):
    """GET - deactivate-confirmation modal; gi-repeat diri ang admin guard sa delete_staff() in case naay direct URL hit."""
    conn = get_db_connection()
    try:
        staff_member, error = staff_service.get_staff_for_delete(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("staff/_delete_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/delete", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def delete_staff(id):
    """POST - i-deactivate ang staff account (dili hard-delete). Delegates sa staff_service.deactivate_staff()."""
    conn = get_db_connection()
    try:
        result, error = staff_service.deactivate_staff(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@staff_bp.route("/staff/<int:id>/reactivate-confirm")
@roles_required("admin")
def reactivate_staff_form(id):
    """GET - reactivate-confirmation modal, puli sa native confirm() para mo-match sa uban staff actions."""
    conn = get_db_connection()
    try:
        staff_member, error = staff_service.get_staff_for_reactivate(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("staff/_reactivate_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/reactivate", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def reactivate_staff(id):
    """POST - pag-reactivate ug bisan unsa nga role, kinahanglan ang acting admin's own password plus a logged reason."""
    data, error = get_json_or_error()
    if error:
        return error

    current_password = (data.get("current_password") or "").strip()
    if not current_password:
        return jsonify({"error": "Enter your password to confirm this change"}), 400
    if not check_password_hash(g.staff["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "A reason is required to reactivate this account"}), 400

    conn = get_db_connection()
    try:
        result, error = staff_service.reactivate_staff(conn, id, g.staff["id"], reason)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@staff_bp.route("/staff/bulk-deactivate", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def bulk_deactivate_staff():
    """POST - bulk deactivate ug daghang staff; admin accounts, i-skip, kay peer-approval ra ang way pag-leave para nila."""
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
        result = staff_service.bulk_deactivate_staff(conn, ids, g.staff["id"])
    finally:
        conn.close()

    return jsonify(result)


@staff_bp.route("/staff/bulk-reactivate", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def bulk_reactivate_staff():
    """POST - bulk reactivate ug daghang staff; walay admin guard needed kay walay peer-approval sa reactivate."""
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
        result = staff_service.bulk_reactivate_staff(conn, ids, g.staff["id"])
    finally:
        conn.close()

    return jsonify(result)


# # # API ROUTES (barcode-based staff lookup for login) -----------------


@staff_bp.route("/staff/barcode/<string:barcode>")
@login_required
@limiter.limit("40 per minute")
def get_staff_by_barcode(barcode):
    """GET - staff lookup by barcode para sa login flow. Delegates to staff_service.get_staff_by_barcode()."""
    conn = get_db_connection()
    try:
        result, error = staff_service.get_staff_by_barcode(conn, barcode)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


# # # ADMIN DEACTIVATION (approval-gated) ----------------------------------


@staff_bp.route("/staff/<int:id>/request-deactivation-form")
@roles_required("admin")
def request_deactivation_form(id):
    """GET - peer-approval deactivation-request modal; gi-repeat diri ang same guards nga gina-enforce sa submit route."""
    conn = get_db_connection()
    try:
        result, error = staff_service.get_staff_for_deactivation_request(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return render_template(
        "staff/_request_deactivation_modal.html",
        staff_member=result["staff_member"],
        already_pending=result["already_pending"],
    )


@staff_bp.route("/staff/<int:id>/request-deactivation", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def request_admin_deactivation(id):
    """POST - mag-request ug deactivation sa laing admin instead of unilateral action; kinahanglan pa ug approvals."""
    data, error = get_json_or_error()
    if error:
        return error

    reason = data.get("reason")
    if not reason:
        return jsonify({"error": "Missing required field: reason"}), 400

    conn = get_db_connection()
    try:
        result, error = staff_service.request_admin_deactivation(conn, id, g.staff, reason)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result), 201


@staff_bp.route("/admin-deactivation-requests/<int:id>/approve", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def approve_admin_deactivation(id):
    """POST - mag-cast ug approve vote sa deactivation request. Delegates sa staff_service.approve_admin_deactivation()."""
    data, error = get_json_or_error()
    reason = data.get("reason") if not error else None
    if not reason:
        return jsonify({"error": "A reason is required to approve this request"}), 400

    conn = get_db_connection()
    try:
        result, error = staff_service.approve_admin_deactivation(conn, id, g.staff, reason)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@staff_bp.route("/admin-deactivation-requests/<int:id>/deny", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def deny_admin_deactivation(id):
    """POST - recorded vote ra ni nga deny; ang requester ra ang makagpa-resolve gyud, either finalize or cancel."""
    data, error = get_json_or_error()
    denial_reason = data.get("denial_reason") if not error else None

    conn = get_db_connection()
    try:
        result, error = staff_service.deny_admin_deactivation(conn, id, g.staff, denial_reason)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@staff_bp.route("/admin-deactivation-requests/<int:id>/finalize", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def finalize_admin_deactivation_route(id):
    """POST - ang requester's own final say; i-deactivate ang target, pero kung sila ra gyud ang mag-pull trigger."""
    conn = get_db_connection()
    try:
        result, error = staff_service.finalize_admin_deactivation_request(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@staff_bp.route("/admin-deactivation-requests/<int:id>/reviews")
@roles_required("admin")
def list_admin_deactivation_reviews(id):
    """GET - tanang votes sa usa ka request, kinsa ug ngano, admin-only ni; ang uban roles aggregate counts ra makita."""
    conn = get_db_connection()
    try:
        result, error = staff_service.get_admin_deactivation_reviews(conn, id)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    if request.headers.get("HX-Request") == "true":
        return render_template("staff/_deactivation_reviews_modal.html", staff=g.staff, **result)

    return jsonify({"results": [dict(r) for r in result["reviews"]]})


@staff_bp.route("/admin-deactivation-requests")
@login_required
def list_admin_deactivation_requests():
    """GET - tanang roles makakita sa tanang requests karon; ang tiers naga-gate lang kinsa pwede mo-vote, dili kinsa makakita."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        context, error = staff_service.list_admin_deactivation_requests(conn, request.args, g.staff["id"], limit, offset)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    if request.headers.get("HX-Request") == "true":
        return render_template("requests/_deactivation_table.html", staff=g.staff, **context)

    return jsonify(
        {
            "total_count": context["total_count"],
            "limit": context["limit"],
            "offset": context["offset"],
            "results": [dict(row) for row in context["requests_list"]],
        }
    )


# # # LOAN LIMITS (per-member-type concurrent-loan caps) ----------------------


@staff_bp.route("/loan-limits/manage-form")
@roles_required("admin", "in_charge")
def manage_loan_limits_form():
    """GET - opens ang Loan Limits modal, current per-member-type caps."""
    conn = get_db_connection()
    try:
        limits = staff_service.get_loan_limits(conn)
    finally:
        conn.close()
    return render_template("staff/_loan_limits_modal.html", limits=limits, error=None)


@staff_bp.route("/loan-limits/update", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def update_loan_limits():
    """POST - i-update ang per-member-type loan caps nga gina-basa sa get_max_concurrent_loans(); pareho ka type i-save dungan."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    updates, error_msg = staff_service.parse_loan_limit_updates(data)
    if error_msg:
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        conn = get_db_connection()
        try:
            limits = staff_service.get_loan_limits(conn)
        finally:
            conn.close()
        return render_template("staff/_loan_limits_modal.html", limits=limits, error=error_msg), 200

    conn = get_db_connection()
    try:
        result = staff_service.save_loan_limits(conn, updates, g.staff["id"])
    finally:
        conn.close()

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # BULK IMPORT (CSV) ------------------------------------------------------


@staff_bp.route("/staff/import-form")
@roles_required("admin")
def import_staff_form():
    """GET - opens ang CSV import modal para sa staff."""
    return render_template("staff/_import_modal.html", error=None)


def _staff_import_error_response(error_msg, status):
    """Mirrors assets.py's _import_error_response - tan-awa didto's docstring
    kung ngano gikinahanglan ang HX-Retarget/HX-Reswap."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    response = make_response(render_template("staff/_import_modal.html", error=error_msg), 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@staff_bp.route("/staff/import", methods=["POST"])
@roles_required("admin")
@limiter.limit("5 per minute")
def import_staff_csv():
    """POST - bulk-create ug staff gikan sa CSV; kada row kinahanglan mag-reference sa existing eligible member, dili bag-o."""
    if "file" not in request.files:
        return _staff_import_error_response("Missing file upload (field name: 'file')", 400)

    file = request.files["file"]
    if not file.filename:
        return _staff_import_error_response("No file selected", 400)

    mode = request.form.get("mode", "commit")
    if mode not in ("preview", "commit"):
        return _staff_import_error_response("mode must be 'preview' or 'commit'", 400)

    try:
        content = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return _staff_import_error_response("File must be UTF-8 encoded CSV", 400)

    result, error = staff_service.import_staff_csv(content, mode, g.staff["id"])

    if error:
        error_payload, status, _ctx = error
        return _staff_import_error_response(error_payload.get("error"), status)

    if result["dry_run"]:
        return render_template(
            "staff/_import_preview.html", rows_to_create=result["rows_to_create"], errors=result["errors"]
        )

    if request.is_json:
        return (
            jsonify({"created_count": result["created_count"], "created": result["created"], "errors": result["errors"]}),
            201 if result["created"] else 400,
        )

    response = make_response(
        render_template(
            "staff/_import_results.html",
            created_count=result["created_count"],
            created=result["created"],
            errors=result["errors"],
        )
    )
    if result["created"]:
        response.headers["HX-Trigger"] = "staff-refreshed"
    return response


# # # EXPORT --------------------------------------------------------------


@staff_bp.route("/staff/export-form")
@roles_required("admin")
def export_staff_form():
    """GET - opens ang export modal; admin-only ni, mo-match sa uban mutations sa page ug sa Staff tab itself."""
    show_inactive = request.args.get("show_inactive") == "1"
    return render_template(
        "staff/_export_modal.html",
        show_inactive=show_inactive,
        default_filename=staff_service.default_staff_export_basename(show_inactive),
        default_export_dir=default_export_dir(),
        fields=[(col, staff_service.STAFF_EXPORT_FIELD_LABELS[col]) for col in staff_service.STAFF_EXPORT_COLUMNS],
    )


@staff_bp.route("/staff/export", methods=["GET", "POST"])
@roles_required("admin")
def export_staff():
    """GET/POST - mirrors export_assets/export_members; admin-only diri, unlike sa duha nga login_required ra."""
    show_inactive = request.values.get("show_inactive") == "1"
    export_format = request.values.get("format", "csv").lower()
    if export_format not in ("csv", "xlsx"):
        export_format = "csv"

    columns = staff_service.resolve_staff_export_columns(request.values.getlist("fields"))

    conn = get_db_connection()
    try:
        staff_rows = staff_service.get_staff_for_export(conn, show_inactive)
    finally:
        conn.close()

    basename = sanitize_export_filename(
        request.values.get("filename"), staff_service.default_staff_export_basename(show_inactive)
    )

    file_bytes, content_type, export_format = staff_service.build_staff_export_file(
        staff_rows, columns, export_format, show_inactive
    )
    filename = f"{basename}.{export_format}"

    response = make_response(file_bytes)
    response.headers["Content-Type"] = content_type
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"

    if request.method == "POST":
        save_dir, was_clamped = resolve_export_dir(request.values.get("save_path"))
        saved_path, warning = staff_service.save_export_copy(file_bytes, save_dir, filename)
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


# # # SESSION SETTINGS (admin-editable login-session timeouts) --------------


@staff_bp.route("/session-settings/manage-form")
@roles_required("admin")
def manage_session_settings_form():
    """GET - opens ang Session Settings modal, current idle/absolute timeout values."""
    return render_template(
        "staff/_session_settings_modal.html",
        session_idle_minutes=get_session_idle_minutes(),
        session_absolute_hours=get_session_absolute_hours(),
        error=None,
    )


@staff_bp.route("/session-settings/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_session_settings():
    """POST - i-update ang idle-timeout ug absolute session-lifetime values nga gina-basa sa require_session() at runtime."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_session_settings(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_session_settings_modal.html",
                session_idle_minutes=get_session_idle_minutes(),
                session_absolute_hours=get_session_absolute_hours(),
                error=error_msg,
            ),
            200,
        )

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # ACCOUNT SECURITY SETTINGS (admin-editable lockout policy) --------------


@staff_bp.route("/account-security/manage-form")
@roles_required("admin")
def manage_account_security_form():
    """GET - opens ang Account Security modal, current failed-attempt threshold ug lockout duration."""
    return render_template(
        "staff/_account_security_modal.html",
        max_failed_attempts=get_max_failed_attempts(),
        lockout_minutes=get_lockout_minutes(),
        error=None,
    )


@staff_bp.route("/account-security/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_account_security():
    """POST - i-update ang failed-attempt threshold ug lockout duration nga gina-basa sa login() at runtime."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_account_security(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_account_security_modal.html",
                max_failed_attempts=get_max_failed_attempts(),
                lockout_minutes=get_lockout_minutes(),
                error=error_msg,
            ),
            200,
        )

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # NOTIFICATION SETTINGS (admin-editable due-soon threshold) -------------


@staff_bp.route("/notification-settings/manage-form")
@roles_required("admin", "in_charge")
def manage_notification_settings_form():
    """GET - opens ang Notification Settings modal, current due-soon threshold."""
    return render_template(
        "staff/_notification_settings_modal.html", due_soon_hours=get_due_soon_hours(), error=None
    )


@staff_bp.route("/notification-settings/update", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def update_notification_settings():
    """POST - i-update ang due-soon threshold nga gina-basa sa sync_loan_notifications() para i-time ang 'due_soon' alert."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_notification_settings(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_notification_settings_modal.html", due_soon_hours=get_due_soon_hours(), error=error_msg
            ),
            200,
        )

    if request.is_json:
        return jsonify(result)
    # No HX-Redirect, unlike other settings modals; this opens from the bell on any page too.
    return make_response("", 200)


# # # LOAN DURATION (admin-editable default checkout due date) --------------


@staff_bp.route("/loan-duration/manage-form")
@roles_required("admin", "in_charge")
def manage_loan_duration_form():
    """GET - opens ang Loan Duration modal, current default loan days."""
    return render_template(
        "staff/_loan_duration_modal.html", default_loan_days=get_default_loan_days(), error=None
    )


@staff_bp.route("/loan-duration/update", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def update_loan_duration():
    """POST - i-update ang fallback due-date length nga gigamit kung walay due date na-pick sa checkout."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_loan_duration(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_loan_duration_modal.html", default_loan_days=get_default_loan_days(), error=error_msg
            ),
            200,
        )

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # CHECKOUT SESSION TIMEOUT (admin/in_charge-editable stale-cart auto-release) --


@staff_bp.route("/checkout-session-settings/manage-form")
@roles_required("admin", "in_charge")
def manage_checkout_session_settings_form():
    """GET - opens ang Checkout Session Timeout modal, current stale-minutes value."""
    return render_template(
        "staff/_checkout_session_settings_modal.html",
        checkout_session_stale_minutes=get_checkout_session_stale_minutes(),
        error=None,
    )


@staff_bp.route("/checkout-session-settings/update", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def update_checkout_session_settings():
    """POST - i-update kung unsa kadugay pwede mag-sit untouched ang in-progress
    checkout cart before ma-auto-cancel ug ma-release balik ang reserved assets."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_checkout_session_settings(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_checkout_session_settings_modal.html",
                checkout_session_stale_minutes=get_checkout_session_stale_minutes(),
                error=error_msg,
            ),
            200,
        )

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # MAINTENANCE SETTINGS (admin/in_charge-editable scheduling defaults) ----


@staff_bp.route("/maintenance-settings/manage-form")
@roles_required("admin", "in_charge")
def manage_maintenance_settings_form():
    """GET - opens ang Maintenance Settings modal, current interval ug reminder-lead-days."""
    return render_template(
        "staff/_maintenance_settings_modal.html",
        default_maintenance_interval_days=get_default_maintenance_interval_days(),
        maintenance_reminder_lead_days=get_maintenance_reminder_lead_days(),
        error=None,
    )


@staff_bp.route("/maintenance-settings/update", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def update_maintenance_settings():
    """POST - i-update ang duha ka app-wide preventive-maintenance defaults -
    kung unsa kalayo ma-auto-compute ang next_maintenance_due sa new/re-serviced
    asset, ug pila ka days before due ang "due soon" sa dashboard/notification sweep."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_maintenance_settings(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_maintenance_settings_modal.html",
                default_maintenance_interval_days=get_default_maintenance_interval_days(),
                maintenance_reminder_lead_days=get_maintenance_reminder_lead_days(),
                error=error_msg,
            ),
            200,
        )

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # DAMAGE RESTRICTION (admin-editable auto-restrict occurrence threshold) --


@staff_bp.route("/damage-restriction/manage-form")
@roles_required("admin", "in_charge")
def manage_damage_restriction_form():
    """GET - opens ang Damage Restriction modal, current occurrence threshold."""
    return render_template(
        "staff/_damage_restriction_modal.html",
        damage_restriction_threshold=get_damage_restriction_threshold(),
        error=None,
    )


@staff_bp.route("/damage-restriction/update", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def update_damage_restriction():
    """POST - i-update pila ka damaged returns ang mo-trigger sa check_and_restrict_damaged_returns()'s auto-restriction."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_damage_restriction(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_damage_restriction_modal.html",
                damage_restriction_threshold=get_damage_restriction_threshold(),
                error=error_msg,
            ),
            200,
        )

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # FEATURE ACCESS (admin-editable tab/workflow toggles) --------------------


@staff_bp.route("/feature-flags/manage-form")
@roles_required("admin")
def manage_feature_flags_form():
    """GET - opens ang Feature Access modal, current on/off state sa tanang feature flags."""
    return render_template("staff/_feature_flags_modal.html", feature_flags=get_feature_flags(), error=None)


@staff_bp.route("/feature-flags/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_feature_flags():
    """POST - i-flip ang feature-flag toggles; kinahanglan ang admin's password either direction, enable man o disable."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    def _rerender_with_error(error_msg, status=400):
        if request.is_json:
            return jsonify({"error": error_msg}), status
        return (
            render_template("staff/_feature_flags_modal.html", feature_flags=get_feature_flags(), error=error_msg),
            200,
        )

    current_password = (data.get("current_password") or "").strip()
    if not current_password:
        return _rerender_with_error("Enter your current password to confirm this change")
    if not check_password_hash(g.staff["password_hash"], current_password):
        return _rerender_with_error("Current password is incorrect", status=401)

    # Handles both: JSON sends real booleans; the htmx form only sends flag_<name> when checked.
    if request.is_json:
        submitted = data.get("flags") or {}
        new_values = {name: bool(submitted.get(name, current)) for name, current in get_feature_flags().items()}
    else:
        new_values = {name: (f"flag_{name}" in data) for name in FEATURE_FLAGS}

    conn = get_db_connection()
    try:
        result = staff_service.update_feature_flags(conn, new_values, g.staff["id"])
    finally:
        conn.close()

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # ALARM SETTINGS ---------------------------------------------------------


@staff_bp.route("/alarm-settings/manage-form")
@roles_required("admin")
def manage_alarm_settings_form():
    """GET - opens ang Alarm Settings modal, current enabled state sa exit-monitoring alarm."""
    return render_template("staff/_alarm_settings_modal.html", enabled=get_alarm_enabled(), error=None)


@staff_bp.route("/alarm-settings/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_alarm_settings():
    """POST - kinahanglan ang admin's password, same sa feature-flags'
    update_feature_flags - ang pag-mute sa exit-monitoring alarm is exactly
    ang tipo sa security-relevant toggle nga dili dapat one-click-away lang."""
    data, error = get_json_or_error()
    if error:
        return error

    current_password = (data.get("current_password") or "").strip()
    if not current_password:
        return jsonify({"error": "Enter your current password to confirm this change"}), 400
    if not check_password_hash(g.staff["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_alarm_settings(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        return jsonify({"error": error_msg}), 400
    return jsonify(result)


# # # BARCODE ID PREFIXES -----------------------------------------------------


@staff_bp.route("/barcode-prefixes/manage-form")
@roles_required("admin")
def manage_barcode_prefixes_form():
    """GET - opens ang Barcode ID Prefixes modal, current prefixes para sa asset/member/staff codes."""
    return render_template(
        "staff/_barcode_prefixes_modal.html",
        asset_id_prefix=get_asset_id_prefix(),
        member_code_prefix=get_member_code_prefix(),
        staff_code_prefix=get_staff_code_prefix(),
        error=None,
    )


@staff_bp.route("/barcode-prefixes/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_barcode_prefixes():
    """POST - ang force_regenerate mag-rewrite pa gyud sa existing records'
    own code, dili lang future ones - kinahanglan ang admin's password to
    confirm, same convention sa Feature Access/Alarm Settings, kay ang mga
    naka-print nga barcode labels dili na maka-scan sa insakto nga record after."""
    data, error = get_json_or_error()
    if error:
        return error

    force_regenerate = bool(data.get("force_regenerate"))
    if force_regenerate:
        current_password = (data.get("current_password") or "").strip()
        if not current_password:
            return jsonify({"error": "Enter your password to confirm updating existing codes"}), 400
        if not check_password_hash(g.staff["password_hash"], current_password):
            return jsonify({"error": "Current password is incorrect"}), 401

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_barcode_prefixes(conn, data, g.staff["id"], force_regenerate)
    finally:
        conn.close()

    if error_msg:
        return jsonify({"error": error_msg}), 400
    return jsonify(result)


# # # BARCODE LABEL ACTIVITY LOGGING ---------------------------------------
# Barcode Labels renders client-side; these endpoints just log the audit trail the server never sees.


@staff_bp.route("/barcodes/log-generate", methods=["POST"])
@roles_required("admin", "in_charge")
def log_barcode_generate():
    """POST - i-log kung unsa gyud nga assets/members ang gi-pick pag-open sa barcode label preview."""
    data, error = get_json_or_error()
    if error:
        return error

    items = data.get("items") or []
    if not items:
        return jsonify({"error": "items is required"}), 400

    staff_service.log_barcode_generate(items, g.staff["id"])
    return jsonify({"logged": True})


@staff_bp.route("/barcodes/log-download", methods=["POST"])
@roles_required("admin", "in_charge")
def log_barcode_download():
    """POST - mirrors log_barcode_generate, ma-call after ma-download gyud ang PDF, separate entry ni sa audit trail."""
    data, error = get_json_or_error()
    if error:
        return error

    items = data.get("items") or []
    if not items:
        return jsonify({"error": "items is required"}), 400
    size = data.get("size") or "regular"
    if size not in ("small", "regular", "large"):
        size = "regular"

    staff_service.log_barcode_download(items, size, g.staff["id"])
    return jsonify({"logged": True})


# # # ONLINE STAFF ----------------------------------------------------------


def get_online_staff(conn, roles=None):
    """Thin wrapper ra ni sa staff_service.get_online_staff() - gigamit sa laing modules nga naga-import diri."""
    return staff_service.get_online_staff(conn, roles=roles)


@staff_bp.route("/staff/online")
@roles_required("admin", "in_charge")
def view_online_staff():
    """GET - backs ang "Currently Online" widget, gina-poll every 30s; in_charge makakita pud sa peers, dili tool_keeper ra."""
    conn = get_db_connection()
    try:
        roles = None if g.staff["role"] == "admin" else ("in_charge", "tool_keeper")
        online_staff = staff_service.get_online_staff(conn, roles=roles)
    finally:
        conn.close()
    return render_template(
        "dashboard/_online_staff.html", online_staff=online_staff, viewer_role=g.staff["role"], viewer_id=g.staff["id"]
    )


# # # DATABASE BACKUPS -------------------------------------------------------
# Admin-only System card for the backup job; backup_db.py is the source of truth for storage.


@staff_bp.route("/backup-settings/manage-form")
@roles_required("admin")
def manage_backup_settings_form():
    """GET - opens ang Database Backups card, current settings ug list sa backup files."""
    conn = get_db_connection()
    try:
        context = staff_service.get_backup_settings_context(conn)
    finally:
        conn.close()
    return render_template("staff/_backup_settings_modal.html", **context)


@staff_bp.route("/backup-settings/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_backup_settings():
    """POST - i-update ang backup schedule/enabled toggle. Delegates sa staff_service.update_backup_settings()."""
    data, error = get_json_or_error()
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_backup_settings(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        return jsonify({"error": error_msg}), 400
    return jsonify(result)


@staff_bp.route("/backup-settings/run-now", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def run_backup_now_route():
    """POST - manual backup; mo-run gyud unconditionally, bisan unsa ang
    enabled toggle o schedule interval. Runs sa background thread, so
    202 dayon ang response (backup pa lang na-start, wala pa nahuman) -
    frontend mag-poll sa manage-form para makita kung human na."""
    result, error_msg = staff_service.trigger_manual_backup(g.staff["id"])
    if error_msg:
        return jsonify({"error": error_msg}), 500
    return jsonify(result), 202


@staff_bp.route("/backup-settings/download/<filename>")
@roles_required("admin")
def download_backup(filename):
    """GET - i-download ang usa ka backup file, naay path-containment check sa resolve_backup_path()."""
    path, error_msg, status = staff_service.resolve_backup_path(filename)
    if error_msg:
        return jsonify({"error": error_msg}), status
    return send_file(path, as_attachment=True, download_name=filename)


@staff_bp.route("/backup-settings/delete-confirm/<filename>")
@roles_required("admin")
def delete_backup_confirm(filename):
    """GET - password-confirmation modal para sa delete_backup, mo-match sa uban permanent actions' shape."""
    _path, error_msg, status = staff_service.resolve_backup_path(filename)
    if error_msg:
        return jsonify({"error": error_msg}), status
    return render_template("staff/_backup_delete_modal.html", filename=filename)


@staff_bp.route("/backup-settings/delete/<filename>", methods=["DELETE"])
@roles_required("admin")
@limiter.limit("20 per minute")
def delete_backup(filename):
    """DELETE - manual removal sa usa ka backup file, same path-containment check sa download; permanent, walay undo."""
    data, error = get_json_or_error()
    if error:
        return error
    current_password = (data.get("current_password") or "").strip()
    if not current_password:
        return jsonify({"error": "Enter your password to confirm this deletion"}), 400
    if not check_password_hash(g.staff["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    backups, error_msg, status = staff_service.delete_backup_file(filename, g.staff["id"])
    if error_msg:
        return jsonify({"error": error_msg}), status

    # Re-rendered list swaps into #backup-file-list; no full reload needed just to drop one row.
    return render_template("staff/_backup_file_list.html", backups=backups)


# # # ARCHIVE AUTO-PURGE --------------------------------------------------------
# Admin-only card for the shared retention window before archive_purge_service permanently deletes records.


@staff_bp.route("/archive-purge-settings/manage-form")
@roles_required("admin")
def manage_archive_purge_settings_form():
    """GET - opens ang Archive Auto-Purge modal, current retention window ug counts sa archived records."""
    conn = get_db_connection()
    try:
        purge_days = get_archive_purge_days(conn)
        counts = staff_service.archived_record_counts(conn)
    finally:
        conn.close()
    return render_template(
        "staff/_archive_purge_settings_modal.html",
        purge_days=purge_days,
        counts=counts,
        presets=staff_service.ARCHIVE_PURGE_PRESETS_DAYS,
    )


@staff_bp.route("/archive-purge-settings/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_archive_purge_settings():
    """POST - i-update ang shared retention window before archive_purge_service permanently deletes records."""
    data, error = get_json_or_error()
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error_msg = staff_service.update_archive_purge_settings(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        return jsonify({"error": error_msg}), 400
    return jsonify(result)


@staff_bp.route("/archive-purge-settings/purge-now", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def run_archive_purge_now():
    """POST - manual purge; mo-run sa same three passes sa scheduled job, pero kung naay retention window na-set ra."""
    conn = get_db_connection()
    try:
        result, error_msg = staff_service.run_archive_purge(conn, g.staff["id"])
    finally:
        conn.close()

    if error_msg:
        return jsonify({"error": error_msg}), 400
    return jsonify(result)
