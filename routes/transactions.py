from flask import Blueprint, jsonify, g, request, render_template, make_response
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required, roles_required
from services.settings_service import is_feature_enabled, get_feature_label, get_default_loan_days
from services import transaction_service
from utils.validators import validate_due_date, parse_pagination_params
from datetime import datetime
from extensions import limiter

"""Thin HTTP layer para sa /transactions ug /checkout-sessions endpoints -
validation ug business logic naa sa services/transaction_service.py, dinhi
lang ang request parsing, response shaping, ug role gates."""

transactions_bp = Blueprint("transactions", __name__)


@transactions_bp.before_request
def _block_if_transactions_disabled():
    """Mag-block sa tanang /transactions* ug /checkout-sessions* request
    kung disabled ang Transactions feature toggle."""
    if not is_feature_enabled("transactions"):
        return jsonify({"error": f"{get_feature_label('transactions')} is currently disabled"}), 403


# # # LIST / VIEW ------------------------------------------------------------


@transactions_bp.route("/transactions")
@login_required
def list_transactions():
    """GET - paginated JSON list sa tanang transactions."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = transaction_service.list_transactions(conn, limit, offset)
    finally:
        conn.close()

    return jsonify(result)


@transactions_bp.route("/transactions/overdue")
@login_required
def list_overdue_transactions():
    """GET - JSON list sa tanang overdue transactions, walay pagination."""
    conn = get_db_connection()
    try:
        result = transaction_service.list_overdue_transactions(conn)
    finally:
        conn.close()

    return jsonify(result)


# # # LIST / VIEW (htmx table partial) --------------------------------------


def get_transactions_table_context(args):
    """Naay kaugalingong connection; gi-share ni sa view_transactions_table()
    diri ug sa transactions_page() sa routes/pages.py, para parehas gihapon."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    conn = get_db_connection()
    try:
        return transaction_service.get_transactions_table_context(conn, args, limit, offset)
    finally:
        conn.close()


@transactions_bp.route("/transactions/table")
@login_required
def view_transactions_table():
    """GET - htmx table partial para sa Transactions page's filterable list."""
    context, error = get_transactions_table_context(request.args)
    if error:
        return error
    return render_template("transactions/_table.html", **context)


@transactions_bp.route("/transactions/export")
@login_required
def export_transactions():
    """GET - CSV sa current filtered table, ignoring sort/pagination kay
    ang export dapat ang tibuok set."""
    conn = get_db_connection()
    try:
        file_bytes, error = transaction_service.build_transactions_export_csv(conn, request.args)
    finally:
        conn.close()

    if error:
        return error

    filename = f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response = make_response(file_bytes)
    response.headers["Content-Type"] = "text/csv"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@transactions_bp.route("/transactions/stat-cards")
@login_required
def transaction_stat_cards():
    """GET - ang 4 ka stat-card numbers as HTML; refreshed via
    'transactions-refreshed' HX-Trigger event, same pattern sa
    /assets/stat-cards."""
    conn = get_db_connection()
    try:
        stats = transaction_service.get_transaction_stat_cards(conn)
    finally:
        conn.close()

    return render_template("transactions/_stat_cards.html", **stats)


@transactions_bp.route("/transactions/<int:id>/details-modal")
@login_required
def transaction_details_modal(id):
    """GET - read-only details modal para sa usa ka transaction row."""
    conn = get_db_connection()
    try:
        transaction, error = transaction_service.get_transaction_details(conn, id)
    finally:
        conn.close()

    if error:
        return error
    return render_template("transactions/_details_modal.html", transaction=transaction)


# # # BORROW (API action) -----------------------------------------------


@transactions_bp.route("/checkout-sessions/start-form")
@login_required
def checkout_start_form():
    """GET - New Checkout modal shell; ang client_id kay gi-mint
    client-side para dili gyud mag-collide sa laing instance."""
    client_id = request.args.get("client_id")
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400
    return render_template("transactions/_checkout_modal.html", client_id=client_id)


@transactions_bp.route("/checkout-sessions/active-banner")
@login_required
def active_checkout_banner():
    """GET - mo-tell sa staff kung naa ba silay in-progress checkout;
    always re-fetched gikan sa server, dili cached client-side."""
    conn = get_db_connection()
    try:
        active_session = transaction_service.load_open_session_for_staff(g.staff["id"], conn)
    finally:
        conn.close()
    return render_template(
        "transactions/_active_checkout_banner.html", active_session=active_session
    )


@transactions_bp.route("/checkout-sessions/dock")
@login_required
def checkout_dock():
    """GET - mo-run sa every page load sa base.html; mag-render ug usa ka
    dock per open session, dili lang ang pinaka-bag-o."""
    conn = get_db_connection()
    try:
        resume_sessions = transaction_service.load_open_sessions_for_staff(g.staff["id"], conn)
    finally:
        conn.close()

    return "".join(
        render_template(
            "transactions/_checkout_modal.html",
            client_id=resume_session["id"],
            resume_session=resume_session,
            auto_minimize=True,
        )
        for resume_session in resume_sessions
    )


@transactions_bp.route("/checkout-sessions/<int:id>/resume-form")
@login_required
def checkout_resume_form(id):
    """GET - mo-reopen sa checkout modal para sa Resume; scoped per staff
    member, dili ma-peek ang session sa uban."""
    conn = get_db_connection()
    try:
        resume_session = transaction_service.load_open_session_for_staff(g.staff["id"], conn)
    finally:
        conn.close()

    if resume_session is None or resume_session["id"] != id:
        return jsonify({"error": "This checkout session is no longer open"}), 409

    return render_template(
        "transactions/_checkout_modal.html",
        client_id=resume_session["id"],
        resume_session=resume_session,
    )


# # # CHECKOUT SESSIONS (start) -------------------------------------------


@transactions_bp.route("/checkout-sessions", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def create_checkout_session():
    """POST - mag-start ug bag-ong checkout session (ang "cart") para sa
    usa ka member; pwede member_barcode or member_id."""
    data, error = get_json_or_error()
    if error:
        return error

    member_barcode = data.get("member_barcode")
    member_id = data.get("member_id")

    if not member_barcode and not member_id:
        return jsonify({"error": "Provide member_barcode or member_id"}), 400

    conn = get_db_connection()
    try:
        result, error = transaction_service.start_checkout_session(conn, member_barcode, member_id, g.staff["id"])
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 201


@transactions_bp.route("/checkout-sessions/available-assets")
@login_required
def list_available_assets_for_checkout():
    """GET - backs the "Show available assets" flyout; `code` kay
    barcode_id, capped ang list, naay category included."""
    conn = get_db_connection()
    try:
        result = transaction_service.list_available_assets_for_checkout(conn)
    finally:
        conn.close()
    return jsonify(result)


@transactions_bp.route("/checkout-sessions/available-members")
@login_required
def list_available_members_for_checkout():
    """GET - backs the "Show available members" flyout sa checkout Step 1;
    `code` kay barcode_id."""
    conn = get_db_connection()
    try:
        result = transaction_service.list_available_members_for_checkout(conn)
    finally:
        conn.close()
    return jsonify(result)


@transactions_bp.route("/checkout-sessions/<int:id>/items/add", methods=["POST"])
@login_required
@limiter.limit("60 per minute")
def add_item_to_session(id):
    """POST - mag-add ug asset item sa checkout session (scan-based cart).
    Quantity relevant ra sa consumable assets."""
    data, error = get_json_or_error()
    if error:
        return error

    asset_code = data.get("asset_code")
    if not asset_code:
        return jsonify({"error": "Missing required field: asset_code"}), 400
    rfid_code = data.get("rfid_code") or None
    # Only meaningful for a consumable asset - ignored otherwise, see
    # transaction_service.add_item_to_checkout_session's docstring.
    quantity = data.get("quantity", 1)

    conn = get_db_connection()
    try:
        result, error = transaction_service.add_item_to_checkout_session(
            conn, id, g.staff["id"], asset_code, rfid_code, quantity
        )
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 201


@transactions_bp.route(
    "/checkout-sessions/<int:id>/items/<int:item_id>/remove", methods=["POST"]
)
@login_required
@limiter.limit("60 per minute")
def remove_item_from_session(id, item_id):
    """POST - mag-remove or mag-decrement ug item sa checkout session's
    cart. `decrement_by` optional, consumable-only quantity stepper."""
    # Consumable-only quantity stepper's "-" button; absent/blank means
    # "remove the whole line" (a serialized item's only option), same as
    # before this existed - see remove_item_from_checkout_session's docstring.
    decrement_by = request.args.get("decrement_by") or (request.get_json(silent=True) or {}).get("decrement_by")
    try:
        decrement_by = int(decrement_by) if decrement_by is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "decrement_by must be a whole number"}), 400

    conn = get_db_connection()
    try:
        result, error = transaction_service.remove_item_from_checkout_session(
            conn, id, item_id, g.staff["id"], decrement_by
        )
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@transactions_bp.route("/checkout-sessions/<int:id>")
@login_required
@limiter.limit("20 per minute")
def view_checkout_session(id):
    """GET - JSON snapshot sa current state sa usa ka checkout session
    (ang cart contents)."""
    conn = get_db_connection()
    try:
        result, error = transaction_service.get_checkout_session_detail(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


# # # RETURN (API action) -----------------------------------------------

VALID_RETURN_CONDITIONS = transaction_service.VALID_RETURN_CONDITIONS


@transactions_bp.route("/transactions/return", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def return_asset():
    """POST - scan-based single-asset return, gi-identify sa asset_code +
    member_barcode combo. Delegates sa return_asset_by_scan()."""
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = ["asset_code", "member_barcode"]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    condition_in = data.get("condition_in")
    if condition_in is not None and condition_in not in VALID_RETURN_CONDITIONS:
        return (
            jsonify(
                {"error": f"condition_in must be one of: {', '.join(VALID_RETURN_CONDITIONS)}"}
            ),
            400,
        )

    conn = get_db_connection()
    try:
        result, error = transaction_service.return_asset_by_scan(
            conn,
            data["asset_code"],
            data["member_barcode"],
            g.staff["id"],
            condition_in,
            data.get("return_remarks"),
            data.get("rfid_code") or None,
        )
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 200


@transactions_bp.route("/transactions/members-with-loans")
@login_required
def members_with_open_loans():
    """GET - backs the Return Items modal's "Show members with loans"
    flyout."""
    conn = get_db_connection()
    try:
        result = transaction_service.list_members_with_open_loans(conn)
    finally:
        conn.close()
    return jsonify(result)


@transactions_bp.route("/transactions/member-open-loans")
@login_required
def member_open_loans():
    """GET - i-list ang gi-borrow sa member; zero loans kay normal 200 with
    empty list ra, dili error."""
    member_barcode = request.args.get("member_barcode")
    if not member_barcode:
        return jsonify({"error": "member_barcode is required"}), 400

    conn = get_db_connection()
    try:
        result, error = transaction_service.get_member_open_loans(conn, member_barcode)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@transactions_bp.route("/transactions/return-scan-form")
@login_required
def return_scan_form():
    """GET - i-render ang scan-return modal shell - walay DB work diri, ang
    modal mo-drive sa iyaha client-side, same shape sa
    checkout_start_form() above."""
    return render_template("transactions/_return_scan_modal.html")


@transactions_bp.route("/transactions/return-scan-lookup")
@login_required
def return_scan_lookup():
    """GET - read-only per-scan lookup para sa scan-return cart; wala pay
    mutate hangtod mo-submit ang batch-return."""
    asset_code = request.args.get("asset_code")
    member_barcode = request.args.get("member_barcode")
    rfid_code = request.args.get("rfid_code") or None
    if not asset_code or not member_barcode:
        return jsonify({"error": "asset_code and member_barcode are required"}), 400

    conn = get_db_connection()
    try:
        result, error = transaction_service.lookup_return_scan(conn, asset_code, member_barcode, rfid_code)
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@transactions_bp.route("/transactions/<int:id>/return-confirm")
@login_required
def return_confirm(id):
    """GET - mo-open sa row-action return modal. Read-only lookup ra - ang
    actual mutation nahitabo sa return_transaction_by_id() below, pag
    submit."""
    conn = get_db_connection()
    try:
        transaction, error = transaction_service.get_return_confirm_transaction(conn, id)
    finally:
        conn.close()

    if error:
        return error
    return render_template("transactions/_return_confirm.html", transaction=transaction)


@transactions_bp.route("/transactions/<int:id>/return", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def return_transaction_by_id(id):
    """POST - row-action return; skip ang barcode lookup, mo-confirm ra
    kung open pa ang transaction."""
    data, error = get_json_or_error()
    if error:
        return error

    condition_in = data.get("condition_in")
    if condition_in is not None and condition_in not in VALID_RETURN_CONDITIONS:
        return (
            jsonify(
                {"error": f"condition_in must be one of: {', '.join(VALID_RETURN_CONDITIONS)}"}
            ),
            400,
        )

    conn = get_db_connection()
    try:
        result, error = transaction_service.return_transaction_by_id(
            conn, id, g.staff["id"], condition_in, data.get("return_remarks")
        )
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 200


# # # BATCH RETURN (API action) -------------------------------------------


@transactions_bp.route("/transactions/batch-return-confirm")
@login_required
def batch_return_confirm():
    """GET - mo-open sa batch-return para sa checked rows; silently
    mo-drop sa ids nga missing or na-return na."""
    ids = request.args.getlist("transaction_ids", type=int)
    if not ids:
        return jsonify({"error": "No transactions selected"}), 400

    conn = get_db_connection()
    try:
        transactions, error = transaction_service.get_batch_return_candidates(conn, ids)
    finally:
        conn.close()

    if error:
        return error
    return render_template("transactions/_batch_return_confirm.html", transactions=transactions)


@transactions_bp.route("/transactions/batch-return", methods=["POST"])
@login_required
@limiter.limit("10 per minute")
def batch_return_transactions():
    """POST - i-apply ang condition/remarks sa selected transactions;
    best-effort, kung naay usa ka bad row dili ma-abort ang uban."""
    data, error = get_json_or_error()
    if error:
        return error

    ids_raw = data.get("transaction_ids")
    if not isinstance(ids_raw, list) or not ids_raw:
        return jsonify({"error": "transaction_ids must be a non-empty list"}), 400
    if len(ids_raw) > transaction_service.MAX_BATCH_RETURN_SIZE:
        return (
            jsonify({"error": f"Cannot return more than {transaction_service.MAX_BATCH_RETURN_SIZE} at once"}),
            400,
        )

    default_condition_in = data.get("condition_in")
    if default_condition_in is not None and default_condition_in not in VALID_RETURN_CONDITIONS:
        return (
            jsonify(
                {"error": f"condition_in must be one of: {', '.join(VALID_RETURN_CONDITIONS)}"}
            ),
            400,
        )

    items = []  # list of (transaction_id, condition_in)
    for entry in ids_raw:
        if isinstance(entry, bool):
            return jsonify({"error": "transaction_ids entries must be integers or objects"}), 400
        if isinstance(entry, int):
            items.append((entry, default_condition_in))
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), int) and not isinstance(entry.get("id"), bool):
            item_condition = entry.get("condition_in", default_condition_in)
            if item_condition is not None and item_condition not in VALID_RETURN_CONDITIONS:
                return (
                    jsonify(
                        {"error": f"condition_in must be one of: {', '.join(VALID_RETURN_CONDITIONS)}"}
                    ),
                    400,
                )
            items.append((entry["id"], item_condition))
            continue
        return jsonify({"error": "transaction_ids entries must be integers or {id, condition_in} objects"}), 400

    return_remarks = data.get("return_remarks")

    conn = get_db_connection()
    try:
        result, error = transaction_service.batch_return(conn, items, return_remarks, g.staff["id"])
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 200


# # # RENEW / EXTEND DUE DATE (API action) ---------------------------------


@transactions_bp.route("/transactions/<int:id>/renew-form")
@login_required
def renew_form(id):
    """GET - mo-open sa extend-due-date modal - read-only lookup ra, same
    split sa return_confirm()/return_transaction_by_id()."""
    conn = get_db_connection()
    try:
        transaction, error = transaction_service.get_renewable_transaction(conn, id)
    finally:
        conn.close()

    if error:
        return error
    return render_template("transactions/_renew_form.html", transaction=transaction)


@transactions_bp.route("/transactions/<int:id>/renew", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def renew_transaction(id):
    """POST - i-push ang due date (or i-correct) without touching ang
    status; ginagamit ra ang validate_due_date validator."""
    data, error = get_json_or_error()
    if error:
        return error

    new_due_date = data.get("due_date")
    if not new_due_date:
        return jsonify({"error": "Missing required field: due_date"}), 400
    error = validate_due_date(new_due_date)
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = transaction_service.renew_transaction(conn, id, new_due_date, g.staff["id"])
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 200


# # # CHECKOUT SESSIONS (API action) -------------------------------------


@transactions_bp.route("/checkout-sessions/open")
@roles_required("admin", "in_charge")
def list_open_checkout_sessions():
    """GET - admin/in_charge-only JSON list sa tanang open checkout
    sessions across staff, monitoring purposes."""
    conn = get_db_connection()
    try:
        result = transaction_service.list_open_checkout_sessions(conn)
    finally:
        conn.close()
    return jsonify(result)


# # # CHECKOUT SESSIONS - explicit finish -------------------------------


@transactions_bp.route("/checkout-sessions/<int:id>/checkout", methods=["POST"])
@login_required
def checkout_session(id):
    """POST - ang actual commit point - wala pay ma-landing sa
    `transactions` table hangtod ni mo-run; ang tanan gi-scan before ni kay
    reservation ra."""
    data, error = get_json_or_error()
    if error:
        return error

    due_date = data.get("due_date")
    error = validate_due_date(due_date)
    if error:
        return error

    conn = get_db_connection()
    try:
        default_loan_days = get_default_loan_days(conn)
        result, error = transaction_service.finish_checkout_session(
            conn, id, g.staff["id"], due_date, default_loan_days
        )
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result), 201


@transactions_bp.route("/checkout-sessions/<int:id>/cancel", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def cancel_checkout_session(id):
    """POST - i-cancel ang in-progress checkout session (walay commit
    nga nahitabo, safe ra i-discard)."""
    conn = get_db_connection()
    try:
        result, error = transaction_service.cancel_checkout_session(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)
