from flask import Blueprint, render_template, request, g, redirect, url_for
from database import get_db_connection
from services.auth_service import login_required, roles_required, feature_required
from routes.assets import get_assets_table_context  # adjust path if assets.py lives elsewhere
from routes.members import get_members_table_context  # same
from routes.staff import get_staff_table_context  # same
from routes.transactions import get_transactions_table_context  # same
from utils.formatters import full_name
from utils.validators import parse_pagination_params, validate_date_string

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/login")
def login_page():
    error = request.args.get("error")
    username = request.args.get("username", "")
    return render_template("login.html", error=error, username=username)


@pages_bp.route("/setup")
@login_required
def setup_page():
    """One-time first-admin setup wizard, only usable while logged in as the temp
    system-seeded admin (services/seed_service.seed_initial_admin); posts to
    /api/v1/setup/complete which creates the real admin and retires this one.
    No `staff` passed to the template so base.html hides the sidebar/header.
    Anyone else who hits this gets redirected to the dashboard."""
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
    # get_assets_table_context() only returns table/pagination state, not lookup
    # data, so without this the category dropdown stayed empty until an htmx swap.
    categories = conn.execute(
        "SELECT * FROM categories ORDER BY sort_order, name"
    ).fetchall()
    conn.close()

    # Same query logic as view_assets()'s htmx partial, so a hard refresh on a
    # filtered URL (?q=...&sort_by=...&show_archived=1) renders correctly server-side
    # instead of showing defaults until the JS re-syncs the filters.
    table_context, error = get_assets_table_context(request.args)
    if error:
        # Bad query string: fall back to defaults instead of erroring the whole page
        # (an API caller would get a 400 here instead).
        table_context = {
            "assets": [],
            "total_count": 0,
            "limit": 20,
            "offset": 0,
            "show_archived": False,
            "sort_by": "created_at",
            "sort_dir": "desc",
        }

    # NOTE: templates/assets.html is top-level; partials live in templates/assets/.
    return render_template(
        "assets.html",
        staff=g.staff,
        stat_all=stat_all,
        stat_available=stat_available,
        stat_borrowed=stat_borrowed,
        stat_maintenance=stat_maintenance,
        categories=categories,
        # table_context skips q/category_id/status/condition/deleted_from/deleted_to since
        # _table.html doesn't need them, but assets.html does (to mark selected options and
        # fill date inputs) -- pull them from request.args directly.
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

    # Same reasoning as assets_page: reuse view_members()'s query logic so a hard
    # refresh on a filtered URL renders that state server-side instead of defaults.
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

    # NOTE: templates/members.html is top-level; partials live in templates/members/.
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
    # "Locked" only counts an unexpired lockout -- once locked_until passes, the
    # account can log in again so it counts as Active instead (they should still
    # sum to stat_all).
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

    # Same reasoning as members_page/assets_page: reuse view_staff()'s query logic
    # so a filtered hard refresh renders that state instead of defaults.
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

    # NOTE: templates/staff.html is top-level; partials live in templates/staff/.
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
    # "Overdue" = same condition as /transactions/overdue and the is_overdue
    # column in get_transactions_table_context(): still borrowed, past due date.
    stat_overdue = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM transactions
        WHERE status = 'borrowed' AND due_date IS NOT NULL AND due_date < datetime('now', 'localtime')
        """
    ).fetchone()["cnt"]
    stat_returned = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE status = 'returned'"
    ).fetchone()["cnt"]

    # Only set when arriving via a notification's "open" link (?member_id=<id>) --
    # looked up here so transactions.html can show whose loans are filtered.
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

    # Same reasoning as assets_page/members_page/staff_page: reuse
    # view_transactions_table()'s query logic so a filtered hard refresh renders
    # that state instead of defaults.
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

    # NOTE: templates/transactions.html is top-level; partials live in templates/transactions/.
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
    """Table loads via htmx (see notifications.html, routes/notifications.py's
    /notifications/table) instead of being rendered here. This route just needs
    limit/offset to seed the container's initial hx-vals for a paginated URL."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 20, 0
    return render_template("notifications.html", staff=g.staff, limit=limit, offset=offset)


@pages_bp.route("/audit-log")
@login_required
def audit_log_page():
    """The missing page for the sidebar's "Activity Log" link, which used to 404
    since /api/v1/audit-log only returned raw JSON. Table loads via htmx same as
    notifications_page(); this route just seeds limit/offset for the initial hx-vals."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 25, 0

    # Only admins can filter by staff_id (non-admins always get their own
    # entries), so only fetch the staff dropdown options for admins.
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
    """Approvals queue for unrestriction and admin-deactivation requests. Was
    previously unreachable -- the sidebar link pointed here but nothing was
    registered at this path. Not role-gated beyond login: list_unrestriction_requests
    and list_admin_deactivation_requests already scope rows per role."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 20, 0

    # tab + each panel's status/mine/offset are read from the URL (u_/d_ prefixed
    # so both tabs round-trip independently) so a refresh reopens on the right
    # tab/filters instead of resetting to Unrestriction/Pending.
    tab = request.args.get("tab", "unrestriction")
    if tab not in ("unrestriction", "deactivation"):
        tab = "unrestriction"

    valid_statuses = ("pending", "approved", "denied", "cancelled", "")

    def _panel_status(prefix):
        value = request.args.get(f"{prefix}_status", "pending")
        return value if value in valid_statuses else "pending"

    def _panel_offset(prefix):
        try:
            return max(0, int(request.args.get(f"{prefix}_offset", 0)))
        except (TypeError, ValueError):
            return 0

    return render_template(
        "requests.html", staff=g.staff, limit=limit, offset=offset, active_tab=tab,
        u_status=_panel_status("u"), u_offset=_panel_offset("u"), u_mine=request.args.get("u_mine") == "1",
        d_status=_panel_status("d"), d_offset=_panel_offset("d"), d_mine=request.args.get("d_mine") == "1",
    )


@pages_bp.route("/reports")
@roles_required("admin", "in_charge", "tool_keeper")
@feature_required("reports")
def reports_page():
    """Just the page shell -- all the numbers come from stats.py's JSON report
    endpoints, fetched and rendered client-side (no htmx here, since it's JSON
    turned into bars/tables, not an HTML partial to swap in).

    tool_keeper can reach this page too but gets a smaller widget set scoped to
    their own activity only (see services/report_widgets.py).

    since/until are read back from the URL (bad values just get dropped) so a
    refreshed /reports?since=...&until=... keeps that range instead of resetting
    to All Time."""

    def _report_date(param):
        value = request.args.get(param, "")
        if value and validate_date_string(value, param) is not None:
            return ""
        return value

    return render_template(
        "reports.html", staff=g.staff,
        since=_report_date("since"), until=_report_date("until"),
    )


@pages_bp.route("/barcodes")
@roles_required("admin", "in_charge")
def barcodes_page():
    """Barcode label generator -- pick assets/members, preview CODE128 barcodes,
    print via the browser dialog. Reachable only from System (batch utility, no
    sidebar tab). Only "currently present" rows get labels: deleted_at IS NULL
    for assets, is_active = 1 for members. No separate staff list -- a staff
    barcode is just their linked member's.

    Both lists are fetched up front and filtered client-side (small dataset,
    one-time-per-visit picker, no need for htmx round-trips)."""
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
@roles_required("admin", "in_charge")
def system_page():
    """Launcher page for app-wide policy knobs (session timeouts, lockout, loan
    limits, notification threshold) -- just opens existing modal+endpoints, no
    new settings-storage logic here.

    in_charge can reach this page but system.html hides the Session Settings/
    Account Security/Feature Access cards (admin-only security knobs) from them.
    Those three endpoints are still @roles_required("admin") on their own too,
    so hiding the cards isn't the only thing enforcing that."""
    return render_template("system.html", staff=g.staff)


@pages_bp.route("/feedback")
@roles_required("admin")
def feedback_page():
    """Admin-only tab for reviewing feedback submitted via the account dropdown's
    "Give feedback" row (routes/feedback.py: submit_feedback). List loads via
    htmx, same cold-load pattern as Notifications.

    status/category are read from the URL too, not just limit/offset, so a
    filtered refresh (e.g. /feedback?status=resolved) keeps that filter instead
    of resetting to the "Open" default."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 20, 0
    status = request.args.get("status", "open")
    if status not in ("open", "resolved", "all"):
        status = "open"
    category = request.args.get("category", "")
    if category not in ("", "bug", "idea", "other"):
        category = ""
    return render_template(
        "feedback.html", staff=g.staff, limit=limit, offset=offset,
        status=status, category=category,
    )


@pages_bp.route("/help")
@login_required
def help_page():
    """The user manual is a modal now (#full-help-modal in base.html), reachable
    from the account menu instead of its own page. This route just keeps old
    /help bookmarks from 404ing -- redirects with a one-shot query param that
    base.html's JS picks up to open the modal and strip back off the URL."""
    return redirect(url_for("home", open_help=1))
