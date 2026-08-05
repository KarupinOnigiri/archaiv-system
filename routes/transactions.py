from flask import Blueprint, jsonify, g, request, render_template, make_response
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required, roles_required
from services.loan_service import (
    check_and_restrict_overdue_member,
    check_and_lift_restriction_if_resolved,
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


DEFAULT_LOAN_DAYS = 2
MAX_CONCURRENT_LOANS = 10
MAX_AVAILABLE_ASSETS_LISTED = 300


def _get_active_checkout_session(session_id, member_id, conn):
    """Returns (session_row, error_response) for a checkout session that a
    borrow can be attached to.

    A checkout session represents one checkout episode where a member can
    have several assets borrowed against the same session_id (badge scanned
    once, staff scans multiple tools). It stays open until either:
      - staff explicitly finishes it (POST .../finish), or
      - it goes stale (no activity for SESSION_STALE_MINUTES), which this
        function lazily closes the moment it's noticed.

    error_response is None if the session exists, belongs to member_id,
    and is still open.
    """
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
        # lazily close it so it stops showing up as "open" going forward
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
    """Returns an error response if this checkout session belongs to a
    different staff member than the one currently logged in, else None.

    Sessions created before staff_id existed (or with a NULL staff_id for
    any other reason) have no owner on record, so there's nothing to
    compare against -- they're treated as accessible rather than as
    permanently locked out, since that's a data gap, not a real owner
    mismatch."""
    if session["staff_id"] is not None and session["staff_id"] != g.staff["id"]:
        return jsonify({"error": "This checkout session belongs to another staff member"}), 403
    return None


def _load_open_session_for_staff(staff_id, conn):
    """Returns the given staff member's open checkout session, including
    its scanned items -- shaped exactly like `_checkout_modal.html`'s
    resume_session and `_active_checkout_banner.html`'s active_session
    expect. Lazily expires it first if it's gone stale. Returns None if
    they don't have one open.

    This is the single source of truth for "does this staff have an
    in-progress checkout right now" -- used by the active-checkout
    banner, the resume-form, and the global dock in base.html, so a
    session found this way behaves identically no matter which of those
    three asked for it."""
    session = conn.execute(
        """
        SELECT checkout_sessions.*, members.barcode_id AS member_barcode, members.first_name AS member_first_name
        FROM checkout_sessions
        JOIN members ON checkout_sessions.member_id = members.id
        WHERE checkout_sessions.staff_id = ? AND checkout_sessions.status = 'open'
        ORDER BY checkout_sessions.created_at DESC
        LIMIT 1
        """,
        (staff_id,),
    ).fetchone()

    if session is None:
        return None

    last_activity_at = datetime.strptime(
        session["last_activity_at"] or session["created_at"], "%Y-%m-%d %H:%M:%S"
    )
    if datetime.now() - last_activity_at > timedelta(minutes=SESSION_STALE_MINUTES):
        conn.execute(
            "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ? AND status = 'open'",
            (session["id"],),
        )
        conn.commit()
        return None

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

    return {**dict(session), "cart_items": [dict(row) for row in items]}


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
    """Builds the WHERE clause + params shared by the table listing and
    the CSV export -- pulled out so the two can never quietly filter
    differently. Returns (where_clauses, params, status_filter, None) on
    success, or (None, None, None, error_response) on a bad query param."""
    where_clauses = []
    params = []

    q = args.get("q")
    if q:
        where_clauses.append(
            "(assets.name LIKE ? OR assets.asset_id LIKE ? OR members.first_name LIKE ? OR members.last_name LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

    # "overdue" is a virtual status -- not a value actually stored in
    # transactions.status, but a derived condition (still borrowed, past
    # due) staff care about as its own filterable bucket. Kept in sync
    # with how /transactions/overdue and the is_overdue column below both
    # define "overdue", so all three never disagree with each other.
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
    """CSV download of whatever the table currently shows -- respects the
    same search/status/date filters as /transactions/table (built by the
    same _build_transactions_filters(), so this can't silently diverge
    from what's on screen), but ignores sort/pagination since an export is
    the whole filtered set, not one page of it. Read-only, so any
    logged-in staff can use it, same reasoning as assets' export route."""
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
    # utf-8-sig (BOM) so Excel opens accented/special characters correctly
    # when double-clicking the CSV directly -- matches assets' CSV export.
    file_bytes = output.getvalue().encode("utf-8-sig")

    filename = f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response = make_response(file_bytes)
    response.headers["Content-Type"] = "text/csv"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@transactions_bp.route("/transactions/stat-cards")
@login_required
def transaction_stat_cards():
    """Returns just the 4 stat-card numbers as HTML, same pattern as
    /assets/stat-cards -- listened to via the 'transactions-refreshed'
    HX-Trigger event so the cards stay in sync with the table after a
    return, without a full page reload."""
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
    """Renders the New Checkout modal shell. The modal itself drives the
    whole borrow flow client-side via fetch() against the JSON endpoints
    below (create_checkout_session, add_item_to_session,
    remove_item_from_session, checkout_session, cancel_checkout_session) --
    this route has no DB work of its own.

    client_id is minted client-side (crypto.randomUUID()) and sent as a
    query param specifically so this fresh instance gets its own unique
    DOM id/data-client-id -- without it, every "New Checkout" click would
    render with the same blank client_id and collide with whatever
    checkout is already open or minimized (see _checkout_modal.html)."""
    client_id = request.args.get("client_id")
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400
    return render_template("transactions/_checkout_modal.html", client_id=client_id)


@transactions_bp.route("/checkout-sessions/active-banner")
@login_required
def active_checkout_banner():
    """Re-fetched on page load and whenever transactions-refreshed fires
    (see transactions.html). Tells the current staff member whether they
    have an in-progress checkout, independent of anything the frontend
    remembers -- so it's correct even after a full page reload."""
    conn = get_db_connection()
    active_session = _load_open_session_for_staff(g.staff["id"], conn)
    conn.close()
    return render_template(
        "transactions/_active_checkout_banner.html", active_session=active_session
    )


@transactions_bp.route("/checkout-sessions/dock")
@login_required
def checkout_dock():
    """Mounted once in base.html (not per-page), so it runs on every full
    page load regardless of which tab the staff member navigates to.
    Sidebar navigation here is real browser navigation, not an htmx swap
    -- the whole page (and all its JS state) is torn down and rebuilt on
    every click, so an in-progress checkout can only survive that by
    being rediscovered from the server each time, not by keeping any DOM
    node alive across the navigation.

    Renders nothing if the staff member has no open session. Otherwise
    renders the same checkout modal fragment used everywhere else,
    pre-minimized (auto_minimize=True) so it reappears as just the dock
    pill rather than popping the full modal open on every page."""
    conn = get_db_connection()
    resume_session = _load_open_session_for_staff(g.staff["id"], conn)
    conn.close()

    if resume_session is None:
        return ""

    return render_template(
        "transactions/_checkout_modal.html",
        client_id=resume_session["id"],
        resume_session=resume_session,
        auto_minimize=True,
    )


@transactions_bp.route("/checkout-sessions/<int:id>/resume-form")
@login_required
def checkout_resume_form(id):
    """Reopens the checkout modal, expanded, for an in-progress session --
    used by the active-checkout banner's Resume button. Scoped to the
    current staff member via _load_open_session_for_staff rather than a
    direct lookup-by-id, so this can't be used to peek at or resume
    another staff member's session (it'll just report the session as no
    longer open, same as if it had actually completed/expired, rather
    than distinguishing "not yours" from "not open" -- no need to reveal
    which one it is)."""
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
            "SELECT * FROM members WHERE barcode_id = ?", (member_barcode,)
        ).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    if not member["is_active"]:
        conn.close()
        return jsonify({"error": "This member account is not active"}), 403

    # Re-check overdue status now, not just at final commit — staff and
    # the member shouldn't waste time scanning tools into a cart that
    # can never be checked out.
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
    """Backs the "Show available assets" flyout in the checkout modal --
    everything a staff member could scan into a cart right now, so they
    can browse/click instead of only ever typing or scanning a barcode.

    `code` is deliberately barcode_id, not assets.asset_id (the display
    tag shown elsewhere as asset_code) -- add_item_to_session() below only
    ever matches a scanned asset_code against barcode_id OR rfid_id, so
    returning anything else here would let a picked row round-trip back
    through items/add and silently fail to resolve, or worse, resolve to
    a different asset if a rfid_id happens to collide with another row's
    asset_id. "Available" mirrors add_item_to_session()'s own definition
    exactly (status = 'available', not soft-deleted), since this listing
    only means something if it's a promise that items/add will accept it.

    Capped and unpaginated -- this is a picker list for the checkout
    flow, not the full inventory table (that's the paginated/sortable
    GET /assets/table in routes/assets.py). MAX_AVAILABLE_ASSETS_LISTED
    keeps a large inventory from dumping thousands of rows into the
    flyout at once; raise it (or add real search/pagination here, mirroring
    get_transactions_table_context's pattern above) if that cap ever
    turns out too low in practice.

    NOTE: no category column on assets -- dropped it from the SELECT
    (was assumed, not verified against the real schema). Add it back once
    it's clear whether that's a differently-named column here or lives on
    a joined table instead.
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
    """Scans one asset into an open checkout session (the "cart"). Mirrors
    the reservation pattern used at final checkout: claim the asset
    atomically by flipping status available -> reserved, and only insert
    the cart row if that claim actually landed."""
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
        "SELECT * FROM assets WHERE (barcode_id = ? OR rfid_id = ?) AND deleted_at IS NULL",
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
    """Core return logic shared by the barcode-scan return flow
    (/transactions/return, used by kiosk-style scanning) and the
    row-action return flow (/transactions/<id>/return, used from the
    Transactions tab table where staff already has the specific record
    on screen). Mutates the DB and returns a result dict describing what
    happened -- callers are responsible for their own
    conn.commit()/rollback()/close() and for calling log_action()
    themselves, since the two callers log slightly different context
    (scanned vs. row-click) around the same underlying change.
    """
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
        # Disposed is terminal — soft-delete now instead of parking it
        # in 'maintenance', where it'd look identical to something
        # that's coming back into service.
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

    return {
        "final_condition": final_condition,
        "is_disposed": is_disposed,
        "new_status": new_status,
    }


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

    asset = conn.execute(
        "SELECT * FROM assets WHERE (barcode_id = ? OR rfid_id = ?) AND deleted_at IS NULL",
        (data["asset_code"], data["asset_code"]),
    ).fetchone()

    if asset is None:
        conn.close()
        return jsonify({"error": "Asset not found"}), 404

    transaction = conn.execute(
        "SELECT * FROM transactions WHERE asset_id = ? AND status = 'borrowed'",
        (asset["id"],),
    ).fetchone()

    if transaction is None:
        conn.close()
        return jsonify({"error": "This asset has no open borrow transaction"}), 400

    member = conn.execute(
        "SELECT * FROM members WHERE barcode_id = ?", (data["member_barcode"],)
    ).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "member not found"}), 404

    if member["id"] != transaction["member_id"]:
        conn.close()
        return (
            jsonify(
                {"error": "This asset was borrowed by someone else. member mismatch."}
            ),
            403,
        )

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
    """Row-action return flow -- staff already has the specific borrow
    record on screen (via the table row), so unlike /transactions/return
    (the scan-first kiosk flow) this skips the barcode/RFID lookup and
    member-barcode confirmation, and just confirms the transaction is
    still open before finalizing. Shares _finalize_return with that
    route so the two can never quietly diverge on what "returning an
    asset" actually does to the DB."""
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
    """Opens the batch-return modal for whatever row checkboxes are
    currently ticked on the table -- read-only lookup, same split as
    return_confirm()/return_transaction_by_id() above. Silently drops any
    id that's missing or already returned rather than erroring, since the
    selection was built from what the table showed a moment ago and may
    have drifted (e.g. someone else just returned one) by the time this
    modal opens; the confirm screen just shows however many are still
    actually eligible."""
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
    """Applies the same condition/remarks to every selected transaction in
    one submit, reusing _finalize_return per row so batch and single-row
    return can never quietly diverge on what "returning an asset" does to
    the DB. Best-effort: one row failing (already returned out from under
    the selection, bad id, etc.) doesn't abort the rest -- the response
    reports exactly which ids landed and which didn't, and the modal
    surfaces that instead of an all-or-nothing error."""
    data, error = get_json_or_error()
    if error:
        return error

    ids = data.get("transaction_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
        return jsonify({"error": "transaction_ids must be a non-empty list of integers"}), 400
    if len(ids) > MAX_BATCH_RETURN_SIZE:
        return jsonify({"error": f"Cannot return more than {MAX_BATCH_RETURN_SIZE} at once"}), 400

    condition_in = data.get("condition_in")
    if condition_in is not None and condition_in not in VALID_RETURN_CONDITIONS:
        return (
            jsonify(
                {"error": f"condition_in must be one of: {', '.join(VALID_RETURN_CONDITIONS)}"}
            ),
            400,
        )
    return_remarks = data.get("return_remarks")

    conn = get_db_connection()
    returned_ids = []
    skipped_ids = []

    for txn_id in ids:
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
    time_out/status -- the loan stays 'borrowed' throughout. Reuses
    validate_due_date so "a valid, non-past date" means exactly the same
    thing here as it does at checkout time."""
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

    # A renewal that pushes the due date back into the future is exactly
    # the kind of thing that should un-overdue a member -- same check the
    # return flow runs after _finalize_return, reused here so "no longer
    # has anything overdue" means the same thing from either path.
    check_and_lift_restriction_if_resolved(transaction["member_id"], conn)
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="renewed_loan",
        target_table="transactions",
        target_id=id,
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
    """The actual commit point. Nothing lands in `transactions` until this
    is called — everything scanned before this is just a reservation."""
    data, error = get_json_or_error()
    if error:
        return error

    due_date = data.get("due_date")
    error = validate_due_date(due_date)
    if error:
        return error
    if due_date is None:
        due_date = (datetime.now() + timedelta(days=DEFAULT_LOAN_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

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

    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (session["member_id"],)
    ).fetchone()

    # Re-check eligibility at the actual moment of commitment, not just at
    # cart-creation time — status may have changed while scanning.
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

    if current_loan_count + len(items) > MAX_CONCURRENT_LOANS:
        conn.close()
        return (
            jsonify(
                {
                    "error": (
                        f"This checkout would put the member at {current_loan_count + len(items)} "
                        f"concurrent loans, exceeding the maximum of {MAX_CONCURRENT_LOANS}"
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
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="cancelled_checkout_session",
        target_table="checkout_sessions",
        target_id=id,
        details=f"Cancelled checkout session {id}, released reserved assets",
    )

    return jsonify({"message": "Checkout session cancelled"})
