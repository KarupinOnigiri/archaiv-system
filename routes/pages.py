from flask import Blueprint, render_template, request, g, redirect, url_for
from database import get_db_connection
from services.auth_service import login_required, roles_required, feature_required
from routes.assets import get_assets_table_context  # adjust the module path here to match wherever assets.py actually lives in your project (you mentioned routes/assets1.py earlier -- if so, use `from routes.assets1 import get_assets_table_context`)
from routes.members import get_members_table_context  # same note as above -- adjust to match wherever members.py actually lives
from routes.staff import get_staff_table_context  # same note as above -- adjust to match wherever staff.py actually lives
from routes.transactions import get_transactions_table_context  # same note as above -- adjust to match wherever transactions.py actually lives
from utils.formatters import full_name
from utils.validators import parse_pagination_params

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/login")
def login_page():
    error = request.args.get("error")
    username = request.args.get("username", "")
    return render_template("login.html", error=error, username=username)


@pages_bp.route("/setup")
@login_required
def setup_page():
    """One-time first-admin setup wizard -- only meaningful while logged
    in as the temporary system-seeded admin (see
    services/seed_service.seed_initial_admin). Posts to
    POST /api/v1/setup/complete (routes/setup.py), which creates the
    real first admin and retires this temporary one.

    Deliberately doesn't pass `staff` to the template, the same trick
    login.html relies on to render without the normal sidebar/header
    chrome (see base.html's `{% if staff %}` guard around all of it) --
    this account has no real business browsing the rest of the app
    before setup is even done, so there's nothing to navigate to yet.

    Redirects to the dashboard if hit by anyone else (i.e. any session
    that isn't the temp admin) -- an already-set-up system has no
    temp admin logged in, so showing this wizard there would just error
    out on submit instead of doing anything useful."""
    if not g.staff["is_system_seeded"]:
        return redirect(url_for("home"))
    conn = get_db_connection()
    departments = conn.execute(
        "SELECT * FROM departments ORDER BY sort_order, name"
    ).fetchall()
    conn.close()
    return render_template("setup.html", departments=departments)


@pages_bp.route("/assets")
@login_required
@feature_required("assets")
def assets_page():
    conn = get_db_connection()
    stat_all = conn.execute(
        "SELECT COUNT(*) AS cnt FROM assets WHERE deleted_at IS NULL"
    ).fetchone()["cnt"]
    stat_available = conn.execute(
        "SELECT COUNT(*) AS cnt FROM assets WHERE deleted_at IS NULL AND status = 'available'"
    ).fetchone()["cnt"]
    stat_borrowed = conn.execute(
        "SELECT COUNT(*) AS cnt FROM assets WHERE deleted_at IS NULL AND status = 'borrowed'"
    ).fetchone()["cnt"]
    stat_maintenance = conn.execute(
        "SELECT COUNT(*) AS cnt FROM assets WHERE deleted_at IS NULL AND status = 'maintenance'"
    ).fetchone()["cnt"]
    # Previously missing entirely -- assets.html's category filter
    # loops over `categories`, but nothing ever supplied it on a full page
    # load (get_assets_table_context() only returns table/pagination state,
    # not lookup data), so the dropdown silently rendered with just the
    # "Category" placeholder until an htmx swap happened to refresh it.
    # Matches how members_page() already fetches and passes `departments`.
    categories = conn.execute(
        "SELECT * FROM categories ORDER BY sort_order, name"
    ).fetchall()
    conn.close()

    # Reuses the exact same query logic view_assets() uses for htmx partial
    # requests, so this page can never again be missing something
    # _table.html needs (that mismatch is what caused the 'offset' crash).
    # It also means a hard refresh on a URL with ?q=...&sort_by=...&show_archived=1
    # (the kind the client-side URL-sync fix now produces) renders that same
    # state server-side too, instead of silently falling back to page 1 /
    # defaults and only *looking* right after the JS re-syncs the filter
    # controls a moment later.
    table_context, error = get_assets_table_context(request.args)
    if error:
        # A bad/stale query string shouldn't take down the whole page --
        # an API caller gets a 400 from view_assets() for the same
        # situation, but a full page load falls back to sane defaults
        # instead (empty state fully respects those defaults too, so it's
        # equivalent to landing on /assets with no query string at all).
        table_context = {
            "assets": [],
            "total_count": 0,
            "limit": 20,
            "offset": 0,
            "show_archived": False,
            "sort_by": "created_at",
            "sort_dir": "desc",
        }

    # NOTE: matches templates/assets.html (top-level, no subfolder) --
    # partials live in templates/assets/, same layout as members/staff.
    return render_template(
        "assets.html",
        staff=g.staff,
        stat_all=stat_all,
        stat_available=stat_available,
        stat_borrowed=stat_borrowed,
        stat_maintenance=stat_maintenance,
        categories=categories,
        # get_assets_table_context() only returns what _table.html needs
        # (assets/total_count/limit/offset/show_archived/sort_by/sort_dir) --
        # it deliberately doesn't return q/category_id/status/condition/
        # deleted_from/deleted_to, since _table.html never reads those.
        # index.html does, though (to mark the right <option selected> and
        # fill the date inputs), so they're pulled from request.args here
        # directly. Without this, a hard refresh on a filtered URL (e.g.
        # ?category_id=3&status=available&deleted_from=2026-01-01) rendered
        # the right *rows* (table_context already covered that) but every
        # dropdown/date field silently reset to blank, since Jinja just
        # treats an unpassed variable as falsy rather than erroring.
        category_id=request.args.get("category_id"),
        status_filter=request.args.get("status"),
        condition_filter=request.args.get("condition"),
        deleted_from=request.args.get("deleted_from"),
        deleted_to=request.args.get("deleted_to"),
        **table_context,
    )


@pages_bp.route("/members")
@login_required
@feature_required("members")
def members_page():
    conn = get_db_connection()
    stat_all = conn.execute("SELECT COUNT(*) AS cnt FROM members").fetchone()["cnt"]
    stat_active = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE is_active = 1 AND is_restricted = 0"
    ).fetchone()["cnt"]
    stat_restricted = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE is_active = 1 AND is_restricted = 1"
    ).fetchone()["cnt"]
    stat_inactive = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE is_active = 0"
    ).fetchone()["cnt"]
    departments = conn.execute(
        "SELECT * FROM departments ORDER BY sort_order, name"
    ).fetchall()
    conn.close()

    # Same reasoning as assets_page: reuse the exact query logic
    # view_members() uses for htmx partial requests, so this page can never
    # be missing something members/_table.html needs, and a hard refresh on
    # a filtered URL (?q=...&department_id=...&show_inactive=1) renders
    # that same state server-side instead of silently falling back to
    # page 1 / defaults.
    table_context, error = get_members_table_context(request.args)
    if error:
        table_context = {
            "members": [],
            "total_count": 0,
            "limit": 20,
            "offset": 0,
            "show_inactive": False,
            "sort_by": "name",
            "sort_dir": "asc",
        }

    # NOTE: this matches templates/members.html (top-level, no subfolder) --
    # your partials live in templates/members/ but the main page itself does
    # not.
    return render_template(
        "members.html",
        staff=g.staff,
        stat_all=stat_all,
        stat_active=stat_active,
        stat_restricted=stat_restricted,
        stat_inactive=stat_inactive,
        departments=departments,
        **table_context,
    )


@pages_bp.route("/staff")
@roles_required("admin")
def staff_page():
    conn = get_db_connection()
    stat_all = conn.execute("SELECT COUNT(*) AS cnt FROM staff").fetchone()["cnt"]
    # "Locked" means an unexpired lockout on an active account -- a lock
    # that's already past locked_until has effectively self-cleared (the
    # next login attempt will succeed), so it counts as Active here, not
    # Locked, and only Active/Locked/Inactive need to add up to stat_all.
    stat_locked = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM staff
        WHERE is_active = 1
          AND locked_until IS NOT NULL
          AND locked_until > datetime('now', 'localtime')
        """
    ).fetchone()["cnt"]
    stat_active = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM staff
        WHERE is_active = 1
          AND (locked_until IS NULL OR locked_until <= datetime('now', 'localtime'))
        """
    ).fetchone()["cnt"]
    stat_inactive = conn.execute(
        "SELECT COUNT(*) AS cnt FROM staff WHERE is_active = 0"
    ).fetchone()["cnt"]
    departments = conn.execute(
        "SELECT * FROM departments ORDER BY sort_order, name"
    ).fetchall()
    conn.close()

    # Same reasoning as members_page/assets_page: reuse the exact query
    # logic view_staff() uses for htmx partial requests, so a hard refresh
    # on a filtered URL (?q=...&role=...&department_id=...&show_inactive=1)
    # renders that same state server-side instead of falling back to
    # page 1 / defaults.
    table_context, error = get_staff_table_context(request.args)
    if error:
        table_context = {
            "staff_list": [],
            "total_count": 0,
            "limit": 20,
            "offset": 0,
            "show_inactive": False,
            "status": "",
            "sort_by": "name",
            "sort_dir": "asc",
        }

    # NOTE: matches templates/staff.html (top-level, no subfolder) --
    # partials live in templates/staff/, same layout as members.
    return render_template(
        "staff.html",
        staff=g.staff,
        stat_all=stat_all,
        stat_active=stat_active,
        stat_locked=stat_locked,
        stat_inactive=stat_inactive,
        departments=departments,
        **table_context,
    )


@pages_bp.route("/transactions")
@login_required
@feature_required("transactions")
def transactions_page():
    conn = get_db_connection()
    stat_all = conn.execute("SELECT COUNT(*) AS cnt FROM transactions").fetchone()["cnt"]
    stat_borrowed = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE status = 'borrowed'"
    ).fetchone()["cnt"]
    # "Overdue" mirrors the same condition used by /transactions/overdue
    # and by the is_overdue column in get_transactions_table_context() --
    # still borrowed, and past its due date.
    stat_overdue = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM transactions
        WHERE status = 'borrowed' AND due_date IS NOT NULL AND due_date < datetime('now', 'localtime')
        """
    ).fetchone()["cnt"]
    stat_returned = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE status = 'returned'"
    ).fetchone()["cnt"]

    # Only present when arriving via a notification's "open" link
    # (?member_id=<id>) -- looked up here (rather than in
    # get_transactions_table_context, which only needs the id for its
    # WHERE clause) purely so transactions.html can show *whose* loans
    # are being filtered to, instead of an unexplained filtered list.
    filtered_member_name = None
    member_id = request.args.get("member_id")
    if member_id:
        member_row = conn.execute(
            "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?",
            (member_id,),
        ).fetchone()
        if member_row:
            filtered_member_name = full_name(
                member_row["first_name"],
                member_row["last_name"],
                member_row["middle_name"],
                member_row["suffix"],
            )
    conn.close()

    # Same reasoning as assets_page/members_page/staff_page: reuse the
    # exact query logic view_transactions_table() uses for htmx partial
    # requests, so this page can never be missing something
    # transactions/_table.html needs, and a hard refresh on a filtered
    # URL (?q=...&status=overdue&sort_by=due_date) renders that same
    # state server-side instead of silently falling back to defaults.
    table_context, error = get_transactions_table_context(request.args)
    if error:
        table_context = {
            "transactions": [],
            "total_count": 0,
            "limit": 20,
            "offset": 0,
            "status_filter": "",
            "sort_by": "time_out",
            "sort_dir": "desc",
        }

    # NOTE: matches templates/transactions.html (top-level, no subfolder) --
    # partials live in templates/transactions/, same layout as members/staff.
    return render_template(
        "transactions.html",
        staff=g.staff,
        stat_all=stat_all,
        stat_borrowed=stat_borrowed,
        stat_overdue=stat_overdue,
        stat_returned=stat_returned,
        filtered_member_name=filtered_member_name,
        **table_context,
    )


@pages_bp.route("/notifications")
@login_required
def notifications_page():
    """The table itself loads via htmx (hx-trigger="load" on
    #notifications-table-container, see notifications.html and
    routes/notifications.py's /notifications/table) rather than being
    server-rendered here -- one request instead of two (server render +
    the cold-load JS replay other tabs use), and get_notifications_table_
    context is still the single source of truth either way. This route
    only needs limit/offset, to seed the container's initial hx-vals so
    the first load respects a hard-refreshed, paginated URL."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 20, 0
    return render_template("notifications.html", staff=g.staff, limit=limit, offset=offset)


@pages_bp.route("/audit-log")
@login_required
def audit_log_page():
    """The sidebar's "Activity Log" link used to point here while no
    route existed at this exact path -- the actual logic lived at
    /api/v1/audit-log (staff_bp's url_prefix) and returned raw JSON, so
    the link 404'd. This is the missing page. Same one-request-not-two
    reasoning as notifications_page(): the table loads via htmx
    (get_audit_log_context, /api/v1/audit-log/table), so this route only
    needs limit/offset to seed the container's initial hx-vals."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 25, 0

    # Only admins can actually filter by staff_id (see get_audit_log_context
    # -- non-admins get their own entries regardless of what they pass), so
    # the dropdown that lets them pick one is populated here for admins only
    # rather than querying every staff account for a control most viewers
    # would never see.
    staff_options = []
    if g.staff["role"] == "admin":
        conn = get_db_connection()
        staff_options = conn.execute(
            """
            SELECT staff.id, staff.username,
                   members.first_name, members.last_name, members.middle_name, members.suffix
            FROM staff
            JOIN members ON staff.member_id = members.id
            ORDER BY members.last_name, members.first_name
            """
        ).fetchall()
        conn.close()

    return render_template(
        "audit_log.html",
        staff=g.staff,
        limit=limit,
        offset=offset,
        staff_options=staff_options,
    )


@pages_bp.route("/requests")
@login_required
@feature_required("requests")
def requests_page():
    """Approvals queue -- unrestriction requests, admin-deactivation
    requests, and admin-promotion requests. The sidebar's "Approvals"
    link has pointed here all along (base.html's nav_items), but until
    now nothing was actually registered at this path, so it 404'd; the
    request types were otherwise only reachable by hitting their JSON
    list endpoints directly with no page ever linking to them.

    Not role-gated beyond login: list_unrestriction_requests,
    list_admin_deactivation_requests, and list_admin_promotion_requests
    (routes/requests.py, routes/staff.py) each already scope what a given
    role sees -- their own filed requests plus whichever admin-
    deactivation/promotion tier matches their role, vs. everything for
    admin/in_charge -- so every role has something relevant here, just
    not the same rows."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 20, 0
    return render_template("requests.html", staff=g.staff, limit=limit, offset=offset)


@pages_bp.route("/reports")
@roles_required("admin", "in_charge", "tool_keeper")
@feature_required("reports")
def reports_page():
    """Just the page shell -- every number on it comes from stats.py's
    report endpoints (most/least-borrowed assets, most-borrowed
    categories, loan duration, top borrowers, overdue frequency, condition
    changes, staff activity, plus tool_keeper's own-activity-only trio),
    all of which already existed as working JSON APIs with nothing ever
    rendering them. Fetched and rendered client-side (see reports.html's
    own script), same as frontend_roadmap.md's Phase 5 note calls for --
    no htmx here, this is the one page in the app where that's the right
    call, since there's no HTML partial to swap in, just JSON to turn into
    bars/tables.

    tool_keeper is allowed onto this page too (previously admin/in_charge
    only) but sees a deliberately smaller widget set -- see
    services/report_widgets.py; every widget/query available to them is
    scoped to their own activity only, never other staff's or an
    all-borrowers view."""
    return render_template("reports.html", staff=g.staff)


@pages_bp.route("/barcodes")
@roles_required("admin", "in_charge")
def barcodes_page():
    """Barcode label generator -- pick from currently-present assets
    and/or members, preview the rendered (CODE128, via static/js/
    jsbarcode.min.js) barcodes, then print via the browser's own Print
    dialog. Reachable only from the System page (a batch utility, not a
    daily-visit destination -- no sidebar tab, and no toolbar shortcut
    on Inventory/Borrowers either; keeping it to one door in rather than
    scattering entry points). "Currently present" is deliberately
    narrower than "every row that ever existed": deleted_at IS NULL for
    assets (an archived asset has no reason to get a fresh label) and
    is_active = 1 for members (same idea -- a deactivated member/
    borrower isn't getting handed a new ID barcode). A staff account has
    no barcode of its own -- it's always its linked member's -- so
    there's no separate staff list here; printing a staff member's
    barcode just means printing their member row.

    Both lists are fetched up front and filtered/searched client-side
    (see barcodes.html) rather than via htmx round-trips, same reasoning
    reports.html uses: this is a one-time-per-visit picker, not a
    frequently-refreshed table, and a school-toolroom-scale inventory
    (low thousands at most) is small enough to hand to the browser
    in one shot."""
    conn = get_db_connection()
    assets = conn.execute("""
        SELECT id, asset_id, barcode_id, name
        FROM assets
        WHERE deleted_at IS NULL
        ORDER BY asset_id
        """).fetchall()
    members = conn.execute("""
        SELECT members.id, members.member_code, members.barcode_id,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM members
        WHERE members.is_active = 1
        ORDER BY members.member_code
        """).fetchall()
    conn.close()
    return render_template(
        "barcodes.html", staff=g.staff, assets=assets, members=members
    )


@pages_bp.route("/system")
@roles_required("admin")
def system_page():
    """Admin-only hub for the app-wide policy knobs that used to be
    hardcoded Python constants scattered across auth.py/transactions.py/
    notification_service.py -- session timeouts, account lockout, loan
    limits, and the notification due-soon threshold. Just a launcher
    page: each card opens the same modal+endpoint that already existed
    (moved here from the Staff tab for loan limits/session settings, or
    newly added alongside them for lockout/notifications), so there's no
    new settings-storage logic here, only the landing page for it."""
    return render_template("system.html", staff=g.staff)


@pages_bp.route("/feedback")
@roles_required("admin")
def feedback_page():
    """Admin-only tab for reviewing in-app bug/issue reports submitted via
    the account dropdown's "Give feedback" row (routes/feedback.py:
    submit_feedback). The list itself loads via htmx (see feedback.html),
    same cold-load pattern as Notifications -- this route only needs
    limit/offset to seed the container's initial hx-vals."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 20, 0
    return render_template("feedback.html", staff=g.staff, limit=limit, offset=offset)
