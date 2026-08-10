from flask import Blueprint, jsonify, g, request, render_template, make_response
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required, roles_required
from services.settings_service import is_feature_enabled, get_feature_label, get_default_loan_days
from services.loan_service import (
    check_and_restrict_overdue_member,
    check_and_lift_restriction_if_resolved,
    check_and_restrict_damaged_returns,
)
from utils.logger import log_action
from utils.formatters import full_name
from utils.validators import (
    validate_due_date,
    validate_date_string,
    parse_pagination_params,
)
from datetime import datetime, timedelta
from utils.constants import SESSION_STALE_MINUTES
import sqlite3
import csv
import io
from extensions import limiter

transactions_bp = Blueprint("transactions", __name__)


@transactions_bp.before_request
def _block_if_transactions_disabled():
    if not is_feature_enabled("transactions"):
        return jsonify({"error": f"{get_feature_label('transactions')} is currently disabled"}), 403


# Fallback only -- real default lives in app_settings (settings_service.get_default_loan_days)
DEFAULT_LOAN_DAYS = 2
# Fallback only -- real caps live in the loan_limits table (see get_max_concurrent_loans below)
MAX_CONCURRENT_LOANS = 10
MAX_AVAILABLE_ASSETS_LISTED = 300


def get_max_concurrent_loans(member_type, conn):
    """Looks up the concurrent-loan cap for a member_type, falling back to
    MAX_CONCURRENT_LOANS if loan_limits has no row for it."""
    row = conn.execute(
        "SELECT max_concurrent_loans FROM loan_limits WHERE member_type = ?",
        (member_type,),
    ).fetchone()
    return row["max_concurrent_loans"] if row else MAX_CONCURRENT_LOANS


def _get_active_checkout_session(session_id, member_id, conn):
    """Returns (session_row, error_response) for a checkout session that a
    borrow can be attached to. A session covers one checkout episode (badge
    scanned once, multiple tools scanned against it) and stays open until
    staff finish it or it goes stale, which this function lazily closes.
    error_response is None if the session exists, belongs to member_id, and
    is still open."""
    session = conn.execute(
        "SELECT * FROM checkout_sessions WHERE id = ?", (session_id,)
    ).fetchone()

    if session is None:
        return None, (
            jsonify(
                {"error": "session_id does not match an existing checkout session"}
            ),
            404,
        )

    if session["member_id"] != member_id:
        return None, (
            jsonify({"error": "session_id belongs to a different member"}),
            400,
        )

    if session["status"] != "open":
        return None, (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    last_activity_at = datetime.strptime(
        session["last_activity_at"] or session["created_at"], "%Y-%m-%d %H:%M:%S"
    )
    if datetime.now() - last_activity_at > timedelta(minutes=SESSION_STALE_MINUTES):
        # lazily close it so it stops showing up as "open"
        conn.execute(
            "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ? AND status = 'open'",
            (session_id,),
        )
        conn.commit()
        return None, (
            jsonify({"error": "This checkout session has timed out. Start a new one."}),
            409,
        )

    return session, None


def _require_session_owner(session):
    """Errors out if this session belongs to a different staff member than
    the one logged in. Sessions with no staff_id on record (data gap, not a
    real owner) are treated as accessible rather than locked out."""
    if session["staff_id"] is not None and session["staff_id"] != g.staff["id"]:
        return jsonify({"error": "This checkout session belongs to another staff member"}), 403
    return None


def _load_open_sessions_for_staff(staff_id, conn):
    """Returns ALL of the staff member's open checkout sessions (newest
    first) with their scanned items, shaped for `_checkout_modal.html` and
    `_active_checkout_banner.html`. Lazily expires stale ones (excluded
    from the result). Single source of truth for "what's this staff member
    got in progress" -- used by the banner, resume-form, and base.html dock."""
    sessions = conn.execute(
        """
        SELECT checkout_sessions.*, members.barcode_id AS member_barcode, members.first_name AS member_first_name
        FROM checkout_sessions
        JOIN members ON checkout_sessions.member_id = members.id
        WHERE checkout_sessions.staff_id = ? AND checkout_sessions.status = 'open'
        ORDER BY checkout_sessions.created_at DESC
        """,
        (staff_id,),
    ).fetchall()

    results = []
    for session in sessions:
        last_activity_at = datetime.strptime(
            session["last_activity_at"] or session["created_at"], "%Y-%m-%d %H:%M:%S"
        )
        if datetime.now() - last_activity_at > timedelta(minutes=SESSION_STALE_MINUTES):
            conn.execute(
                "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ? AND status = 'open'",
                (session["id"],),
            )
            conn.commit()
            continue

        items = conn.execute(
            """
            SELECT checkout_session_items.id AS id, assets.name AS asset_name
            FROM checkout_session_items
            JOIN assets ON checkout_session_items.asset_id = assets.id
            WHERE checkout_session_items.checkout_session_id = ?
            ORDER BY checkout_session_items.added_at
            """,
            (session["id"],),
        ).fetchall()

        results.append({**dict(session), "cart_items": [dict(row) for row in items]})

    return results


def _load_open_session_for_staff(staff_id, conn):
    """Most recent open session, or None -- thin wrapper around
    _load_open_sessions_for_staff for callers that only want one.
    checkout_dock uses the plural version directly."""
    sessions = _load_open_sessions_for_staff(staff_id, conn)
    return sessions[0] if sessions else None


# # # LIST / VIEW ------------------------------------------------------------


@transactions_bp.route("/transactions")
@login_required
def list_transactions():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    total_count = conn.execute("SELECT COUNT(*) AS cnt FROM transactions").fetchone()[
        "cnt"
    ]
    transactions = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        ORDER BY transactions.time_out DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()
    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in transactions],
        }
    )


@transactions_bp.route("/transactions/overdue")
@login_required
def list_overdue_transactions():
    conn = get_db_connection()
    overdue = conn.execute("""
        SELECT transactions.*, assets.name AS asset_name, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.status = 'borrowed'
        AND transactions.due_date IS NOT NULL
        AND transactions.due_date < datetime('now', 'localtime')
        ORDER BY transactions.due_date ASC
        """).fetchall()
    conn.close()
    return jsonify([dict(row) for row in overdue])


# # # LIST / VIEW (htmx table partial) --------------------------------------

TRANSACTIONS_SORTABLE_COLUMNS = {
    "time_out": "transactions.time_out",
    "due_date": "transactions.due_date",
    "time_in": "transactions.time_in",
    "status": "transactions.status",
    "asset_name": "assets.name",
    "member_name": "members.last_name",
}


def _build_transactions_filters(args):
    """Builds the WHERE clause + params shared by the table listing and the
    CSV export so they can't drift apart. Returns (where_clauses, params,
    status_filter, None) on success, or (None, None, None, error_response)
    on a bad query param."""
    where_clauses = []
    params = []

    # member_id is an exact-match filter for deep-links (e.g. notifications' overdue/due_soon
    # alerts) -- avoids name-collision false matches from the free-text `q` search below.
    member_id = args.get("member_id")
    if member_id:
        try:
            member_id = int(member_id)
        except (TypeError, ValueError):
            return None, None, None, (
                jsonify({"error": "member_id must be an integer"}),
                400,
            )
        where_clauses.append("transactions.member_id = ?")
        params.append(member_id)

    q = args.get("q")
    if q:
        where_clauses.append(
            "(assets.name LIKE ? OR assets.asset_id LIKE ? OR members.first_name LIKE ? OR members.last_name LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

    # "overdue" is a virtual status (borrowed + past due), not a real value in transactions.status --
    # keep this definition in sync with /transactions/overdue and the is_overdue column below.
    status_filter = args.get("status")
    if status_filter == "overdue":
        where_clauses.append(
            "transactions.status = 'borrowed' AND transactions.due_date IS NOT NULL AND transactions.due_date < datetime('now', 'localtime')"
        )
    elif status_filter in ("borrowed", "returned"):
        where_clauses.append("transactions.status = ?")
        params.append(status_filter)

    time_out_from = args.get("time_out_from")
    if time_out_from:
        error = validate_date_string(time_out_from, "time_out_from")
        if error:
            return None, None, None, error
        where_clauses.append("date(transactions.time_out) >= ?")
        params.append(time_out_from)

    time_out_to = args.get("time_out_to")
    if time_out_to:
        error = validate_date_string(time_out_to, "time_out_to")
        if error:
            return None, None, None, error
        where_clauses.append("date(transactions.time_out) <= ?")
        params.append(time_out_to)

    return where_clauses, params, status_filter, None


def get_transactions_table_context(args):
    """Builds everything transactions/_table.html needs (transactions,
    total_count, limit, offset, status_filter, sort_by, sort_dir) from a
    request.args-like object. Shared by view_transactions_table() (the
    htmx partial endpoint) and transactions_page() in routes/pages.py --
    same reasoning as get_assets_table_context() in routes/assets.py, so
    the two can't drift out of sync on what the template requires.

    Returns (context_dict, None) on success, or (None, error_response)
    on a bad query param.
    """
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    where_clauses, params, status_filter, error = _build_transactions_filters(args)
    if error:
        return None, error

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    sort_by = args.get("sort_by", "time_out")
    if sort_by not in TRANSACTIONS_SORTABLE_COLUMNS:
        return None, (
            jsonify(
                {"error": f"sort_by must be one of: {', '.join(TRANSACTIONS_SORTABLE_COLUMNS)}"}
            ),
            400,
        )
    sort_dir = args.get("sort_dir", "desc").lower()
    if sort_dir not in ("asc", "desc"):
        return None, (jsonify({"error": "sort_dir must be 'asc' or 'desc'"}), 400)
    sort_column = TRANSACTIONS_SORTABLE_COLUMNS[sort_by]

    conn = get_db_connection()
    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        {where_sql}
        """,
        params,
    ).fetchone()["cnt"]

    transactions = conn.execute(
        f"""
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               borrowed_staff.username AS borrowed_by_username,
               returned_staff.username AS returned_by_username,
               CASE WHEN transactions.status = 'borrowed'
                    AND transactions.due_date IS NOT NULL
                    AND transactions.due_date < datetime('now', 'localtime')
               THEN 1 ELSE 0 END AS is_overdue
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        LEFT JOIN staff AS borrowed_staff ON transactions.borrowed_by_staff_id = borrowed_staff.id
        LEFT JOIN staff AS returned_staff ON transactions.returned_by_staff_id = returned_staff.id
        {where_sql}
        ORDER BY {sort_column} {sort_dir.upper()}, transactions.id {sort_dir.upper()}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    conn.close()

    return {
        "transactions": transactions,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "status_filter": status_filter or "",
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }, None


@transactions_bp.route("/transactions/table")
@login_required
def view_transactions_table():
    context, error = get_transactions_table_context(request.args)
    if error:
        return error
    return render_template("transactions/_table.html", **context)


TRANSACTIONS_EXPORT_COLUMNS = [
    ("asset_code", "Asset Code"),
    ("asset_name", "Asset"),
    ("member_name", "Member"),
    ("time_out", "Time Out"),
    ("due_date", "Due Date"),
    ("time_in", "Return Date"),
    ("status", "Status"),
    ("condition_out", "Condition Out"),
    ("condition_in", "Condition In"),
    ("borrowed_by_username", "Checked Out By"),
    ("returned_by_username", "Returned To"),
    ("return_remarks", "Remarks"),
]


@transactions_bp.route("/transactions/export")
@login_required
def export_transactions():
    """CSV download of whatever the table currently shows -- same filters
    as /transactions/table, but ignores sort/pagination since export means
    the whole filtered set. Read-only, so any logged-in staff can use it."""
    where_clauses, params, _status_filter, error = _build_transactions_filters(request.args)
    if error:
        return error
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    conn = get_db_connection()
    rows = conn.execute(
        f"""
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               borrowed_staff.username AS borrowed_by_username,
               returned_staff.username AS returned_by_username
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        LEFT JOIN staff AS borrowed_staff ON transactions.borrowed_by_staff_id = borrowed_staff.id
        LEFT JOIN staff AS returned_staff ON transactions.returned_by_staff_id = returned_staff.id
        {where_sql}
        ORDER BY transactions.time_out DESC
        """,
        params,
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in TRANSACTIONS_EXPORT_COLUMNS])
    for row in rows:
        record = dict(row)
        record["member_name"] = full_name(
            row["first_name"], row["last_name"], row["middle_name"], row["suffix"]
        )
        writer.writerow(
            [record.get(col) if record.get(col) is not None else "" for col, _ in TRANSACTIONS_EXPORT_COLUMNS]
        )
    # BOM so Excel handles accented characters right on direct double-click open
    file_bytes = output.getvalue().encode("utf-8-sig")

    filename = f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response = make_response(file_bytes)
    response.headers["Content-Type"] = "text/csv"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@transactions_bp.route("/transactions/stat-cards")
@login_required
def transaction_stat_cards():
    """The 4 stat-card numbers as HTML; refreshed via the
    'transactions-refreshed' HX-Trigger event, same pattern as /assets/stat-cards."""
    conn = get_db_connection()
    stat_all = conn.execute("SELECT COUNT(*) AS cnt FROM transactions").fetchone()["cnt"]
    stat_borrowed = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE status = 'borrowed'"
    ).fetchone()["cnt"]
    stat_overdue = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM transactions
        WHERE status = 'borrowed' AND due_date IS NOT NULL AND due_date < datetime('now', 'localtime')
        """
    ).fetchone()["cnt"]
    stat_returned = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE status = 'returned'"
    ).fetchone()["cnt"]
    conn.close()
    return render_template(
        "transactions/_stat_cards.html",
        stat_all=stat_all,
        stat_borrowed=stat_borrowed,
        stat_overdue=stat_overdue,
        stat_returned=stat_returned,
    )


@transactions_bp.route("/transactions/<int:id>/details-modal")
@login_required
def transaction_details_modal(id):
    conn = get_db_connection()
    transaction = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               borrower_staff.username AS borrowed_by_username,
               returner_staff.username AS returned_by_username
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        LEFT JOIN staff AS borrower_staff ON transactions.borrowed_by_staff_id = borrower_staff.id
        LEFT JOIN staff AS returner_staff ON transactions.returned_by_staff_id = returner_staff.id
        WHERE transactions.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()

    if transaction is None:
        return jsonify({"error": "Transaction not found"}), 404

    return render_template("transactions/_details_modal.html", transaction=transaction)


# # # BORROW (API action) -----------------------------------------------


@transactions_bp.route("/checkout-sessions/start-form")
@login_required
def checkout_start_form():
    """Renders the New Checkout modal shell; the modal drives the borrow
    flow client-side via fetch() against the JSON endpoints below, no DB
    work here. client_id is minted client-side (crypto.randomUUID()) so
    each modal instance gets a unique DOM id and doesn't collide with an
    already-open/minimized checkout."""
    client_id = request.args.get("client_id")
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400
    return render_template("transactions/_checkout_modal.html", client_id=client_id)


@transactions_bp.route("/checkout-sessions/active-banner")
@login_required
def active_checkout_banner():
    """Tells the current staff member whether they have an in-progress
    checkout, re-fetched from the server (not frontend state) so it's
    correct even after a full page reload."""
    conn = get_db_connection()
    active_session = _load_open_session_for_staff(g.staff["id"], conn)
    conn.close()
    return render_template(
        "transactions/_active_checkout_banner.html", active_session=active_session
    )


@transactions_bp.route("/checkout-sessions/dock")
@login_required
def checkout_dock():
    """Mounted once in base.html so it runs on every full page load --
    sidebar nav is real browser navigation, so an in-progress checkout has
    to be rediscovered from the server each time, not kept alive in the DOM.
    Renders one dock per open session (not just the most recent) since a
    staff member can have several in flight at once; using the singular
    loader here used to lose the dock for any but the newest session after
    navigating away. Each fragment starts pre-minimized (auto_minimize=True).
    Renders nothing if there are no open sessions."""
    conn = get_db_connection()
    resume_sessions = _load_open_sessions_for_staff(g.staff["id"], conn)
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
    """Reopens the checkout modal, expanded, for the resume banner's Resume
    button. Scoped by staff member (not a direct lookup-by-id) so you can't
    peek at another staff member's session -- it just reports "not open"
    either way, no distinction leaked."""
    conn = get_db_connection()
    resume_session = _load_open_session_for_staff(g.staff["id"], conn)
    conn.close()

    if resume_session is None or resume_session["id"] != id:
        return jsonify({"error": "This checkout session is no longer open"}), 409

    return render_template(
        "transactions/_checkout_modal.html",
        client_id=resume_session["id"],
        resume_session=resume_session,
    )


# # # CHECKOUT SESSION ITEMS (the "cart") --------------------------------


# # # CHECKOUT SESSIONS (start) -------------------------------------------


@transactions_bp.route("/checkout-sessions", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def create_checkout_session():
    """Starts a new checkout session (the "cart") for a member — the entry
    point for the borrow flow. Items get scanned into it afterward via
    /checkout-sessions/<id>/items/add, then committed via
    /checkout-sessions/<id>/checkout.

    Accepts either member_barcode (the normal scan-first kiosk flow) or
    member_id (e.g. starting a session from a member's profile page) —
    same either/or pattern as /login."""
    data, error = get_json_or_error()
    if error:
        return error

    member_barcode = data.get("member_barcode")
    member_id = data.get("member_id")

    if not member_barcode and not member_id:
        return jsonify({"error": "Provide member_barcode or member_id"}), 400

    conn = get_db_connection()

    if member_id:
        member = conn.execute(
            "SELECT * FROM members WHERE id = ?", (member_id,)
        ).fetchone()
    else:
        member = conn.execute(
            "SELECT * FROM members WHERE UPPER(barcode_id) = UPPER(?)", (member_barcode,)
        ).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    if not member["is_active"]:
        conn.close()
        return jsonify({"error": "This member account is not active"}), 403

    # re-check overdue status now, not just at final commit, so staff don't scan a cart that can't check out
    check_and_restrict_overdue_member(member["id"], conn)
    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (member["id"],)
    ).fetchone()

    if member["is_restricted"]:
        conn.close()
        return (
            jsonify(
                {
                    "error": "This member is restricted",
                    "reason": member["restriction_reason"],
                }
            ),
            403,
        )

    existing_open = conn.execute(
        "SELECT id FROM checkout_sessions WHERE member_id = ? AND status = 'open'",
        (member["id"],),
    ).fetchone()

    if existing_open is not None:
        conn.close()
        return (
            jsonify(
                {
                    "error": "This member already has an open checkout session",
                    "checkout_session_id": existing_open["id"],
                }
            ),
            409,
        )

    try:
        cursor = conn.execute(
            "INSERT INTO checkout_sessions (member_id, staff_id, last_activity_at) VALUES (?, ?, datetime('now', 'localtime'))",
            (member["id"], g.staff["id"]),
        )
        conn.commit()
        session_id = cursor.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="started_checkout_session",
        target_table="checkout_sessions",
        target_id=session_id,
        target_name=full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix']),
        details=(
            f"Started checkout session for member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}"
        ),
    )

    return (
        jsonify(
            {
                "message": "Checkout session started",
                "checkout_session_id": session_id,
                "member_id": member["id"],
                "member_first_name": member["first_name"],
            }
        ),
        201,
    )


@transactions_bp.route("/checkout-sessions/available-assets")
@login_required
def list_available_assets_for_checkout():
    """Backs the "Show available assets" flyout in the checkout modal so
    staff can browse/click instead of only scanning a barcode.

    `code` is barcode_id, not assets.asset_id -- add_item_to_session()
    below matches a scanned code against barcode_id OR rfid_id, so
    returning asset_id here could round-trip back and fail to resolve, or
    resolve to the wrong asset. "Available" mirrors
    add_item_to_session()'s own definition (status='available', not
    soft-deleted) since this list is only useful as a promise items/add
    will honor.

    Capped via MAX_AVAILABLE_ASSETS_LISTED and unpaginated -- this is a
    picker for the checkout flow, not the full inventory table (that's
    GET /assets/table).

    NOTE: no category column on assets -- dropped from the SELECT, add
    back once we know its real name/location in the schema.
    """
    conn = get_db_connection()
    assets = conn.execute(
        """
        SELECT barcode_id AS code, name
        FROM assets
        WHERE status = 'available' AND deleted_at IS NULL
        ORDER BY name
        LIMIT ?
        """,
        (MAX_AVAILABLE_ASSETS_LISTED,),
    ).fetchall()
    conn.close()
    return jsonify({"assets": [dict(row) for row in assets]})


@transactions_bp.route("/checkout-sessions/<int:id>/items/add", methods=["POST"])
@login_required
@limiter.limit("60 per minute")
def add_item_to_session(id):
    """Scans one asset into an open checkout session (the "cart"). Claims
    the asset atomically (available -> reserved) and only inserts the cart
    row if that claim lands, same reservation pattern as final checkout."""
    data, error = get_json_or_error()
    if error:
        return error

    asset_code = data.get("asset_code")
    if not asset_code:
        return jsonify({"error": "Missing required field: asset_code"}), 400

    conn = get_db_connection()

    session_probe = conn.execute(
        "SELECT member_id, staff_id FROM checkout_sessions WHERE id = ?", (id,)
    ).fetchone()

    if session_probe is None:
        conn.close()
        return jsonify({"error": "Checkout session not found"}), 404

    error = _require_session_owner(session_probe)
    if error:
        conn.close()
        return error

    session, error = _get_active_checkout_session(id, session_probe["member_id"], conn)
    if error:
        conn.close()
        return error

    asset = conn.execute(
        "SELECT * FROM assets WHERE (UPPER(barcode_id) = UPPER(?) OR UPPER(rfid_id) = UPPER(?)) AND deleted_at IS NULL",
        (asset_code, asset_code),
    ).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    already_in_cart = conn.execute(
        "SELECT id FROM checkout_session_items WHERE checkout_session_id = ? AND asset_id = ?",
        (id, asset["id"]),
    ).fetchone()

    if already_in_cart is not None:
        conn.close()
        return jsonify({"error": "This asset is already in the cart"}), 409

    if asset["status"] != "available":
        conn.close()
        return (
            jsonify(
                {"error": f"Asset is not available (current status: {asset['status']})"}
            ),
            409,
        )

    try:
        claim = conn.execute(
            "UPDATE assets SET status = 'reserved' WHERE id = ? AND status = 'available'",
            (asset["id"],),
        )
        if claim.rowcount == 0:
            conn.rollback()
            return (
                jsonify(
                    {"error": "Asset was just claimed by another session — try again"}
                ),
                409,
            )

        cursor = conn.execute(
            "INSERT INTO checkout_session_items (checkout_session_id, asset_id) VALUES (?, ?)",
            (id, asset["id"]),
        )
        conn.execute(
            "UPDATE checkout_sessions SET last_activity_at = datetime('now', 'localtime') WHERE id = ?",
            (id,),
        )
        conn.commit()
        item_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({"error": "This asset is already in the cart"}), 409
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="added_item_to_checkout_session",
        target_table="checkout_session_items",
        target_id=item_id,
        target_name=f"{asset['asset_id']} — {asset['name']}",
        details=f"Added asset {asset['asset_id']} to checkout session {id}",
    )

    return (
        jsonify(
            {
                "message": "Item added to cart",
                "item_id": item_id,
                "asset_id": asset["id"],
                "asset_name": asset["name"],
            }
        ),
        201,
    )


@transactions_bp.route(
    "/checkout-sessions/<int:id>/items/<int:item_id>/remove", methods=["POST"]
)
@login_required
@limiter.limit("60 per minute")
def remove_item_from_session(id, item_id):
    conn = get_db_connection()

    item = conn.execute(
        "SELECT * FROM checkout_session_items WHERE id = ? AND checkout_session_id = ?",
        (item_id, id),
    ).fetchone()

    if item is None:
        conn.close()
        return jsonify({"error": "Cart item not found"}), 404

    session = conn.execute(
        "SELECT * FROM checkout_sessions WHERE id = ?", (id,)
    ).fetchone()

    error = _require_session_owner(session)
    if error:
        conn.close()
        return error

    if session["status"] != "open":
        conn.close()
        return (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    asset = conn.execute(
        "SELECT asset_id, name FROM assets WHERE id = ?", (item["asset_id"],)
    ).fetchone()

    try:
        conn.execute("DELETE FROM checkout_session_items WHERE id = ?", (item_id,))
        conn.execute(
            "UPDATE assets SET status = 'available' WHERE id = ? AND status = 'reserved'",
            (item["asset_id"],),
        )
        conn.execute(
            "UPDATE checkout_sessions SET last_activity_at = datetime('now', 'localtime') WHERE id = ?",
            (id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="removed_item_from_checkout_session",
        target_table="checkout_session_items",
        target_id=item_id,
        target_name=f"{asset['asset_id']} — {asset['name']}" if asset else None,
        details=f"Removed asset from checkout session {id}",
    )

    return jsonify({"message": "Item removed from cart"})


@transactions_bp.route("/checkout-sessions/<int:id>")
@login_required
@limiter.limit("20 per minute")
def view_checkout_session(id):
    conn = get_db_connection()

    session = conn.execute(
        """
        SELECT checkout_sessions.*, members.first_name, members.last_name,
               members.middle_name, members.suffix, members.barcode_id AS member_barcode
        FROM checkout_sessions
        JOIN members ON checkout_sessions.member_id = members.id
        WHERE checkout_sessions.id = ?
        """,
        (id,),
    ).fetchone()

    if session is None:
        conn.close()
        return jsonify({"error": "Checkout session not found"}), 404

    error = _require_session_owner(session)
    if error:
        conn.close()
        return error

    items = conn.execute(
        """
        SELECT checkout_session_items.id AS item_id, assets.id AS asset_id,
               assets.asset_id AS asset_code, assets.name AS asset_name,
               checkout_session_items.added_at
        FROM checkout_session_items
        JOIN assets ON checkout_session_items.asset_id = assets.id
        WHERE checkout_session_items.checkout_session_id = ?
        ORDER BY checkout_session_items.added_at
        """,
        (id,),
    ).fetchall()
    conn.close()

    return jsonify({**dict(session), "items": [dict(row) for row in items]})


# # # RETURN (API action) -----------------------------------------------

VALID_RETURN_CONDITIONS = ("functional", "inoperable", "for_repair", "disposed")


def _finalize_return(transaction, asset, member, staff_id, condition_in, return_remarks, conn):
    """Core return logic shared by the scan-return flow
    (/transactions/return) and the row-action return flow
    (/transactions/<id>/return). Mutates the DB and returns a result dict;
    callers own their own commit/rollback/close and log_action() since
    they log different context around the same change."""
    final_condition = condition_in if condition_in else asset["condition"]
    is_disposed = final_condition == "disposed"
    new_status = "available" if final_condition == "functional" else "maintenance"

    conn.execute(
        """
        UPDATE transactions
        SET time_in = datetime('now', 'localtime'), status = 'returned',
            returned_by_staff_id = ?, return_remarks = ?, condition_in = ?
        WHERE id = ?
        """,
        (staff_id, return_remarks, final_condition, transaction["id"]),
    )

    if is_disposed:
        # disposed is terminal -- soft-delete now instead of parking it in 'maintenance'
        conn.execute(
            "UPDATE assets SET status = ?, condition = ?, deleted_at = datetime('now', 'localtime') WHERE id = ?",
            (new_status, final_condition, asset["id"]),
        )
    else:
        conn.execute(
            "UPDATE assets SET status = ?, condition = ? WHERE id = ?",
            (new_status, final_condition, asset["id"]),
        )

    conn.commit()
    check_and_lift_restriction_if_resolved(member["id"], conn)
    if final_condition != "functional":
        check_and_restrict_damaged_returns(member["id"], conn)

    return {
        "final_condition": final_condition,
        "is_disposed": is_disposed,
        "new_status": new_status,
    }


def _find_open_transaction_for_scan(asset_code, member_barcode, conn):
    """Looks up + validates an asset-barcode/member-barcode pair for a
    scan-driven return -- shared by the single-scan route and the
    scan-return cart's per-scan lookup. Pure lookup, doesn't close conn or
    mutate anything. Returns (transaction, asset, member, error_response);
    error_response is None on success."""
    asset = conn.execute(
        "SELECT * FROM assets WHERE (UPPER(barcode_id) = UPPER(?) OR UPPER(rfid_id) = UPPER(?)) AND deleted_at IS NULL",
        (asset_code, asset_code),
    ).fetchone()
    if asset is None:
        return None, None, None, (jsonify({"error": "Asset not found"}), 404)

    transaction = conn.execute(
        "SELECT * FROM transactions WHERE asset_id = ? AND status = 'borrowed'",
        (asset["id"],),
    ).fetchone()
    if transaction is None:
        return None, None, None, (jsonify({"error": "This asset has no open borrow transaction"}), 400)

    member = conn.execute(
        "SELECT * FROM members WHERE UPPER(barcode_id) = UPPER(?)", (member_barcode,)
    ).fetchone()
    if member is None:
        return None, None, None, (jsonify({"error": "member not found"}), 404)

    if member["id"] != transaction["member_id"]:
        return (
            None,
            None,
            None,
            (jsonify({"error": "This asset was borrowed by someone else. member mismatch."}), 403),
        )

    return transaction, asset, member, None


@transactions_bp.route("/transactions/return", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def return_asset():
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

    transaction, asset, member, error = _find_open_transaction_for_scan(
        data["asset_code"], data["member_barcode"], conn
    )
    if error:
        conn.close()
        return error

    try:
        result = _finalize_return(
            transaction, asset, member, g.staff["id"], condition_in, data.get("return_remarks"), conn
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="returned_asset",
        target_table="transactions",
        target_id=asset["id"],
        target_name=f"{asset['asset_id']} — {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
        details=(
            f"Asset {asset['asset_id']} returned by member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])} "
            f"— condition: {result['final_condition']}"
        ),
    )

    if result["is_disposed"]:
        log_action(
            staff_id=g.staff["id"],
            action="disposed_asset",
            target_table="assets",
            target_id=asset["id"],
            target_name=f"{asset['asset_id']} — {asset['name']}",
            details=f"Asset {asset['asset_id']} disposed on return and removed from circulation",
        )

    return (
        jsonify(
            {
                "message": "asset returned successfully",
                "condition": result["final_condition"],
                "status": result["new_status"],
            }
        ),
        200,
    )


@transactions_bp.route("/transactions/member-open-loans")
@login_required
def member_open_loans():
    """Lists everything a member currently has borrowed -- powers the
    scan-return modal's checklist as soon as the member's barcode is
    entered, before any asset is scanned. Zero open loans is a normal 200
    with an empty list, not an error, so the modal can show its own
    "nothing to return" message."""
    member_barcode = request.args.get("member_barcode")
    if not member_barcode:
        return jsonify({"error": "member_barcode is required"}), 400

    conn = get_db_connection()
    member = conn.execute(
        "SELECT * FROM members WHERE UPPER(barcode_id) = UPPER(?)", (member_barcode,)
    ).fetchone()
    if member is None:
        conn.close()
        return jsonify({"error": "member not found"}), 404

    loans = conn.execute(
        """
        SELECT transactions.id AS transaction_id, assets.name AS asset_name,
               assets.barcode_id AS asset_barcode, transactions.due_date
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        WHERE transactions.member_id = ? AND transactions.status = 'borrowed'
        ORDER BY assets.name
        """,
        (member["id"],),
    ).fetchall()
    conn.close()

    return jsonify(
        {
            "member_name": full_name(
                member["first_name"], member["last_name"], member["middle_name"], member["suffix"]
            ),
            "loans": [dict(row) for row in loans],
        }
    )


@transactions_bp.route("/transactions/return-scan-form")
@login_required
def return_scan_form():
    """Renders the scan-return modal shell -- no DB work here, modal
    drives itself client-side, same shape as checkout_start_form() above."""
    return render_template("transactions/_return_scan_modal.html")


@transactions_bp.route("/transactions/return-scan-lookup")
@login_required
def return_scan_lookup():
    """Read-only per-scan lookup for the scan-return modal's cart: given
    an asset barcode and the member barcode entered at the start of that
    session, resolves + validates them via the same
    _find_open_transaction_for_scan() the single-scan kiosk route uses
    (so a scan gets rejected here for exactly the same reasons it would
    be rejected at actual return time), and returns just enough to render
    a cart row. Nothing is mutated -- the actual return only happens when
    the cart is submitted to /transactions/batch-return."""
    asset_code = request.args.get("asset_code")
    member_barcode = request.args.get("member_barcode")
    if not asset_code or not member_barcode:
        return jsonify({"error": "asset_code and member_barcode are required"}), 400

    conn = get_db_connection()
    transaction, asset, member, error = _find_open_transaction_for_scan(asset_code, member_barcode, conn)
    conn.close()
    if error:
        return error

    return jsonify(
        {
            "transaction_id": transaction["id"],
            "asset_name": asset["name"],
            "asset_barcode": asset["barcode_id"],
        }
    )


@transactions_bp.route("/transactions/<int:id>/return-confirm")
@login_required
def return_confirm(id):
    """Opens the row-action return modal. Read-only lookup -- the actual
    mutation happens in return_transaction_by_id() below, on submit."""
    conn = get_db_connection()
    transaction = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               assets.condition AS asset_condition, members.first_name, members.last_name,
               members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()

    if transaction is None:
        return jsonify({"error": "Transaction not found"}), 404
    if transaction["status"] != "borrowed":
        return jsonify({"error": "This transaction has already been returned"}), 400

    return render_template("transactions/_return_confirm.html", transaction=transaction)


@transactions_bp.route("/transactions/<int:id>/return", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def return_transaction_by_id(id):
    """Row-action return flow -- staff already has the record on screen,
    so unlike /transactions/return this skips the barcode/RFID lookup and
    just confirms the transaction is still open before finalizing via the
    shared _finalize_return."""
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

    transaction = conn.execute("SELECT * FROM transactions WHERE id = ?", (id,)).fetchone()
    if transaction is None:
        conn.close()
        return jsonify({"error": "Transaction not found"}), 404
    if transaction["status"] != "borrowed":
        conn.close()
        return jsonify({"error": "This transaction has already been returned"}), 400

    asset = conn.execute(
        "SELECT * FROM assets WHERE id = ?", (transaction["asset_id"],)
    ).fetchone()
    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (transaction["member_id"],)
    ).fetchone()

    try:
        result = _finalize_return(
            transaction, asset, member, g.staff["id"], condition_in, data.get("return_remarks"), conn
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="returned_asset",
        target_table="transactions",
        target_id=transaction["id"],
        target_name=f"{asset['asset_id']} — {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
        details=(
            f"Asset {asset['asset_id']} returned by member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])} "
            f"— condition: {result['final_condition']}"
        ),
    )

    if result["is_disposed"]:
        log_action(
            staff_id=g.staff["id"],
            action="disposed_asset",
            target_table="assets",
            target_id=asset["id"],
            target_name=f"{asset['asset_id']} — {asset['name']}",
            details=f"Asset {asset['asset_id']} disposed on return and removed from circulation",
        )

    return (
        jsonify(
            {
                "message": f"{asset['asset_id']} returned successfully",
                "condition": result["final_condition"],
                "status": result["new_status"],
            }
        ),
        200,
    )


# # # BATCH RETURN (API action) -------------------------------------------

MAX_BATCH_RETURN_SIZE = 50


@transactions_bp.route("/transactions/batch-return-confirm")
@login_required
def batch_return_confirm():
    """Opens the batch-return modal for the checked table rows. Silently
    drops ids that are missing/already returned instead of erroring, since
    the selection may have drifted since the table was loaded -- the
    confirm screen just shows whatever's still eligible."""
    ids = request.args.getlist("transaction_ids", type=int)
    if not ids:
        return jsonify({"error": "No transactions selected"}), 400

    conn = get_db_connection()
    placeholders = ",".join("?" for _ in ids)
    transactions = conn.execute(
        f"""
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.id IN ({placeholders}) AND transactions.status = 'borrowed'
        ORDER BY assets.name
        """,
        ids,
    ).fetchall()
    conn.close()

    if not transactions:
        return jsonify({"error": "None of the selected loans are still open"}), 400

    return render_template("transactions/_batch_return_confirm.html", transactions=transactions)


@transactions_bp.route("/transactions/batch-return", methods=["POST"])
@login_required
@limiter.limit("10 per minute")
def batch_return_transactions():
    """Applies condition/remarks to every selected transaction, reusing
    _finalize_return per row. Best-effort: one row failing doesn't abort
    the rest -- response reports which ids landed vs. got skipped.

    transaction_ids entries can be a plain int (uses the top-level
    condition_in, for the table's checkbox batch return) or
    {"id", "condition_in"} (per-item override, for the scan-return modal
    where staff inspect each scanned item). Both forms can mix in one
    request. return_remarks is always a single value for the whole batch."""
    data, error = get_json_or_error()
    if error:
        return error

    ids_raw = data.get("transaction_ids")
    if not isinstance(ids_raw, list) or not ids_raw:
        return jsonify({"error": "transaction_ids must be a non-empty list"}), 400
    if len(ids_raw) > MAX_BATCH_RETURN_SIZE:
        return jsonify({"error": f"Cannot return more than {MAX_BATCH_RETURN_SIZE} at once"}), 400

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
    returned_ids = []
    skipped_ids = []

    for txn_id, condition_in in items:
        transaction = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()
        if transaction is None or transaction["status"] != "borrowed":
            skipped_ids.append(txn_id)
            continue

        asset = conn.execute(
            "SELECT * FROM assets WHERE id = ?", (transaction["asset_id"],)
        ).fetchone()
        member = conn.execute(
            "SELECT * FROM members WHERE id = ?", (transaction["member_id"],)
        ).fetchone()

        try:
            result = _finalize_return(
                transaction, asset, member, g.staff["id"], condition_in, return_remarks, conn
            )
        except Exception:
            conn.rollback()
            skipped_ids.append(txn_id)
            continue

        returned_ids.append(txn_id)
        log_action(
            staff_id=g.staff["id"],
            action="returned_asset",
            target_table="transactions",
            target_id=transaction["id"],
            target_name=f"{asset['asset_id']} — {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
            details=(
                f"Asset {asset['asset_id']} returned (batch) by member "
                f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])} "
                f"— condition: {result['final_condition']}"
            ),
        )
        if result["is_disposed"]:
            log_action(
                staff_id=g.staff["id"],
                action="disposed_asset",
                target_table="assets",
                target_id=asset["id"],
                target_name=f"{asset['asset_id']} — {asset['name']}",
                details=f"Asset {asset['asset_id']} disposed on return and removed from circulation",
            )

    conn.close()

    return (
        jsonify(
            {
                "message": f"{len(returned_ids)} asset(s) returned"
                + (f", {len(skipped_ids)} skipped" if skipped_ids else ""),
                "returned": returned_ids,
                "skipped": skipped_ids,
            }
        ),
        200,
    )


# # # RENEW / EXTEND DUE DATE (API action) ---------------------------------


@transactions_bp.route("/transactions/<int:id>/renew-form")
@login_required
def renew_form(id):
    """Opens the extend-due-date modal -- read-only lookup, same split as
    return_confirm()/return_transaction_by_id()."""
    conn = get_db_connection()
    transaction = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        WHERE transactions.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()

    if transaction is None:
        return jsonify({"error": "Transaction not found"}), 404
    if transaction["status"] != "borrowed":
        return jsonify({"error": "Only an active loan can be extended"}), 400

    return render_template("transactions/_renew_form.html", transaction=transaction)


@transactions_bp.route("/transactions/<int:id>/renew", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def renew_transaction(id):
    """Pushes a loan's due date out (or corrects it) without touching
    time_out/status. Reuses validate_due_date so the same rules apply
    here as at checkout."""
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
    transaction = conn.execute("SELECT * FROM transactions WHERE id = ?", (id,)).fetchone()
    if transaction is None:
        conn.close()
        return jsonify({"error": "Transaction not found"}), 404
    if transaction["status"] != "borrowed":
        conn.close()
        return jsonify({"error": "Only an active loan can be extended"}), 400

    old_due_date = transaction["due_date"]
    conn.execute("UPDATE transactions SET due_date = ? WHERE id = ?", (new_due_date, id))
    conn.commit()

    # pushing the due date forward can un-overdue a member -- same check the return flow runs
    check_and_lift_restriction_if_resolved(transaction["member_id"], conn)

    # look up asset/member names before closing conn, just for a readable Activity Log entry
    txn_asset = conn.execute(
        "SELECT asset_id FROM assets WHERE id = ?", (transaction["asset_id"],)
    ).fetchone()
    txn_member = conn.execute(
        "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?",
        (transaction["member_id"],),
    ).fetchone()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="renewed_loan",
        target_table="transactions",
        target_id=id,
        target_name=(
            f"{txn_asset['asset_id']} — {full_name(txn_member['first_name'], txn_member['last_name'], txn_member['middle_name'], txn_member['suffix'])}"
            if txn_asset and txn_member
            else None
        ),
        details=f"Due date extended from {old_due_date or '—'} to {new_due_date}",
    )

    return jsonify({"message": "Due date updated", "due_date": new_due_date}), 200


# # # CHECKOUT SESSIONS (API action) -------------------------------------


@transactions_bp.route("/checkout-sessions/open")
@roles_required("admin", "in_charge")
def list_open_checkout_sessions():
    conn = get_db_connection()
    sessions = conn.execute("""
        SELECT checkout_sessions.*, members.first_name, members.last_name,
               members.middle_name, members.suffix, members.barcode_id AS member_barcode,
               (SELECT COUNT(*) FROM checkout_session_items
                WHERE checkout_session_id = checkout_sessions.id) AS item_count
        FROM checkout_sessions
        JOIN members ON checkout_sessions.member_id = members.id
        WHERE checkout_sessions.status = 'open'
        ORDER BY checkout_sessions.created_at ASC
        """).fetchall()
    conn.close()
    return jsonify([dict(row) for row in sessions])


# # # CHECKOUT SESSIONS — explicit finish -------------------------------


@transactions_bp.route("/checkout-sessions/<int:id>/checkout", methods=["POST"])
@login_required
def checkout_session(id):
    """The actual commit point -- nothing lands in `transactions` until
    this runs; everything scanned before it is just a reservation."""
    data, error = get_json_or_error()
    if error:
        return error

    due_date = data.get("due_date")
    error = validate_due_date(due_date)
    if error:
        return error

    conn = get_db_connection()

    if due_date is None:
        due_date = (
            datetime.now() + timedelta(days=get_default_loan_days(conn))
        ).strftime("%Y-%m-%d %H:%M:%S")

    session = conn.execute(
        "SELECT * FROM checkout_sessions WHERE id = ?", (id,)
    ).fetchone()

    if session is None:
        conn.close()
        return jsonify({"error": "Checkout session not found"}), 404

    error = _require_session_owner(session)
    if error:
        conn.close()
        return error

    if session["status"] != "open":
        conn.close()
        return (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (session["member_id"],)
    ).fetchone()

    # re-check eligibility at commit time, not just cart-creation -- status may have changed while scanning
    if check_and_restrict_overdue_member(member["id"], conn):
        conn.close()
        return (
            jsonify(
                {
                    "error": "This member is restricted",
                    "reason": "Has an overdue asset that has not been returned",
                }
            ),
            403,
        )

    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (session["member_id"],)
    ).fetchone()
    if member["is_restricted"]:
        conn.close()
        return (
            jsonify(
                {
                    "error": "This member is restricted",
                    "reason": member["restriction_reason"],
                }
            ),
            403,
        )

    current_loan_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE member_id = ? AND status = 'borrowed'",
        (member["id"],),
    ).fetchone()["cnt"]

    items = conn.execute(
        "SELECT * FROM checkout_session_items WHERE checkout_session_id = ?", (id,)
    ).fetchall()

    if not items:
        conn.close()
        return (
            jsonify(
                {"error": "Cart is empty — scan at least one asset before checking out"}
            ),
            400,
        )

    max_loans = get_max_concurrent_loans(member["member_type"], conn)
    if current_loan_count + len(items) > max_loans:
        conn.close()
        return (
            jsonify(
                {
                    "error": (
                        f"This checkout would put the member at {current_loan_count + len(items)} "
                        f"concurrent loans, exceeding the maximum of {max_loans}"
                    ),
                    "current_loans": current_loan_count,
                    "cart_size": len(items),
                }
            ),
            403,
        )

    try:
        for item in items:
            asset = conn.execute(
                "SELECT * FROM assets WHERE id = ?", (item["asset_id"],)
            ).fetchone()

            claim = conn.execute(
                "UPDATE assets SET status = 'borrowed' WHERE id = ? AND status = 'reserved'",
                (item["asset_id"],),
            )
            if claim.rowcount == 0:
                raise ValueError(f"asset {item['asset_id']} was not in reserved state")

            conn.execute(
                """
                INSERT INTO transactions
                    (checkout_session_id, asset_id, member_id, borrowed_by_staff_id, due_date, condition_out, status)
                VALUES (?, ?, ?, ?, ?, ?, 'borrowed')
                """,
                (
                    id,
                    item["asset_id"],
                    member["id"],
                    g.staff["id"],
                    due_date,
                    asset["condition"],
                ),
            )

        conn.execute(
            "DELETE FROM checkout_session_items WHERE checkout_session_id = ?", (id,)
        )
        conn.execute(
            "UPDATE checkout_sessions SET status = 'completed', completed_at = datetime('now', 'localtime') WHERE id = ?",
            (id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="checked_out_session",
        target_table="checkout_sessions",
        target_id=id,
        target_name=full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix']),
        details=(
            f"Checked out {len(items)} asset(s) to member "
            f"{full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}"
        ),
    )

    return jsonify({"message": "Checkout complete", "items_borrowed": len(items)}), 201


@transactions_bp.route("/checkout-sessions/<int:id>/cancel", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def cancel_checkout_session(id):
    conn = get_db_connection()

    session = conn.execute(
        "SELECT * FROM checkout_sessions WHERE id = ?", (id,)
    ).fetchone()

    if session is None:
        conn.close()
        return jsonify({"error": "Checkout session not found"}), 404

    error = _require_session_owner(session)
    if error:
        conn.close()
        return error

    if session["status"] != "open":
        conn.close()
        return (
            jsonify({"error": f"This checkout session is already {session['status']}"}),
            409,
        )

    try:
        conn.execute(
            """
            UPDATE assets SET status = 'available'
            WHERE id IN (SELECT asset_id FROM checkout_session_items WHERE checkout_session_id = ?)
            AND status = 'reserved'
            """,
            (id,),
        )
        conn.execute(
            "DELETE FROM checkout_session_items WHERE checkout_session_id = ?", (id,)
        )
        conn.execute(
            "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ?",
            (id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cancel_member = conn.execute(
            "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?",
            (session["member_id"],),
        ).fetchone()
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="cancelled_checkout_session",
        target_table="checkout_sessions",
        target_id=id,
        target_name=(
            full_name(cancel_member['first_name'], cancel_member['last_name'], cancel_member['middle_name'], cancel_member['suffix'])
            if cancel_member else None
        ),
        details=f"Cancelled checkout session {id}, released reserved assets",
    )

    return jsonify({"message": "Checkout session cancelled"})
