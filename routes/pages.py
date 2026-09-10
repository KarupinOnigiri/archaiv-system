"""Thin HTTP layer para sa page-shell routes (assets/members/staff/etc) - mo
render lang ug template with initial context, ang actual table data kay
gikan sa htmx partials sa laing routes files."""

from flask import Blueprint, render_template, request, g, redirect, url_for
from database import get_db_connection
from services.auth_service import login_required, roles_required, feature_required, require_session
from services import asset_service, member_service, staff_service, transaction_service, monitoring_service, setup_service, settings_service, backup_service
from routes.assets import get_assets_table_context  # adjust path if assets.py lives elsewhere
from routes.members import get_members_table_context  # same
from routes.staff import get_staff_table_context  # same
from routes.transactions import get_transactions_table_context  # same
from utils.formatters import full_name
from utils.validators import parse_pagination_params, validate_date_string

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/login")
def login_page():
    """GET - login form. Ang `next` param kay ma-validate para same-site
    relative path ra gyud, para dili ma-abuse as open-redirect."""
    error = request.args.get("error")
    username = request.args.get("username", "")
    # Only a same-site relative path is ever honored (routes/auth.py re-checks
    # this itself before actually redirecting there) - an absolute/external
    # `next` would be an open-redirect vector, so anything else is just dropped
    # here rather than carried through to the form.
    next_url = request.args.get("next", "")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = ""
    return render_template("login.html", error=error, username=username, next_url=next_url)


@pages_bp.route("/setup")
def setup_page():
    """GET - First-admin setup wizard. Walay @login_required kay ang true
    fresh install (walay staff pa gyud) walay ma-login as, so open ni siya
    ana nga case. Sa uban case (live system's temp-admin recovery path, see
    services/seed_service.py) required gihapon login as temp admin, checked
    manually diri kay lahi man ang rules sa duha ka case."""
    conn = get_db_connection()
    fresh_install = setup_service.is_fresh_install(conn)

    if not fresh_install:
        staff, error = require_session(conn)
        if error:
            conn.close()
            return redirect(url_for("pages.login_page"))
        if not staff["is_system_seeded"]:
            conn.close()
            return redirect(url_for("home"))

    departments = conn.execute(
        "SELECT * FROM departments ORDER BY sort_order, name"
    ).fetchall()
    conn.close()
    return render_template("setup.html", departments=departments, fresh_install=fresh_install)


@pages_bp.route("/assets")
@login_required
@feature_required("assets")
def assets_page():
    """GET - Assets page shell. Nag-pull ug stat cards + category dropdown
    dayon, ug mo-reuse sa get_assets_table_context() (routes/assets.py) para
    mo-render na dayon ug filtered state kung naay query string sa hard
    refresh, dili default lang."""
    conn = get_db_connection()
    stats = asset_service.get_asset_stat_cards(conn)
    # get_assets_table_context() only returns table/pagination state, not lookup
    # data, so without this the category dropdown stayed empty until an htmx swap.
    categories = conn.execute(
        "SELECT * FROM categories ORDER BY sort_order, name"
    ).fetchall()
    conn.close()

    # Same query logic as view_assets()'s htmx partial, so a filtered hard
    # refresh renders server-side instead of showing defaults.
    table_context, error = get_assets_table_context(request.args)
    if error:
        # Bad query string: fall back to defaults instead of erroring the page.
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
        categories=categories,
        **stats,
        # table_context skips q/category_id/status/condition/deleted_from/deleted_to;
        # assets.html needs them too (selected options, date inputs) so pull from request.args.
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
    """GET - Members page shell, same pattern sa assets_page(): stat cards +
    department dropdown, dayon reuse get_members_table_context()
    (routes/members.py) para sa filtered hard refresh."""
    conn = get_db_connection()
    stats = member_service.get_member_stat_cards(conn)
    departments = conn.execute(
        "SELECT * FROM departments ORDER BY sort_order, name"
    ).fetchall()
    conn.close()

    # Same reasoning as assets_page: reuse view_members()'s query logic so a
    # filtered hard refresh renders that state instead of defaults.
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
        departments=departments,
        **stats,
        **table_context,
    )


@pages_bp.route("/staff")
@roles_required("admin")
def staff_page():
    """GET - Staff page shell, admin-only. Same pattern sa assets_page/
    members_page: stat cards + reuse get_staff_table_context()
    (routes/staff.py) para sa filtered hard refresh."""
    conn = get_db_connection()
    stats = staff_service.get_staff_stat_cards(conn)
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
        departments=departments,
        **stats,
        **table_context,
    )


@pages_bp.route("/transactions")
@login_required
@feature_required("transactions")
def transactions_page():
    """GET - Transactions page shell. Nag-lookup pa ug member name kung
    naay ?member_id= (gikan sa notification's "open" link), dayon reuse
    get_transactions_table_context() (routes/transactions.py) para sa
    filtered hard refresh."""
    conn = get_db_connection()
    # "Overdue" = same condition as get_transactions_table_context()'s
    # is_overdue column: still borrowed, past due date.
    stats = transaction_service.get_transaction_stat_cards(conn)

    # Only set when arriving via a notification's "open" link (?member_id=<id>);
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
        filtered_member_name=filtered_member_name,
        **stats,
        **table_context,
    )


@pages_bp.route("/notifications")
@login_required
def notifications_page():
    """GET - Notifications page shell. Table gikan sa htmx
    (routes/notifications.py's /notifications/table); ni nga route mo-seed
    lang ug limit/offset para sa container's initial hx-vals."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 20, 0
    return render_template("notifications.html", staff=g.staff, limit=limit, offset=offset)


@pages_bp.route("/monitoring")
@roles_required("admin", "in_charge", "tool_keeper")
@feature_required("monitoring")
def monitoring_page():
    """GET - RFID exit-monitoring page shell: live alarm status + log sa
    tagged-tool detection events gikan sa Pi's RFID listener. Table/banner
    load via htmx, same pattern sa Activity Log ug Notifications."""
    conn = get_db_connection()
    stats = monitoring_service.get_monitoring_stat_cards(conn)
    conn.close()

    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 25, 0

    classification = request.args.get("classification", "")
    if classification not in ("", "authorized", "unauthorized", "unknown_tag"):
        classification = ""

    # All time by default (blank since/until) - matches
    # monitoring_service.get_monitoring_stat_cards' own all-time scope now,
    # so the whole page opens on one consistent baseline instead of stat
    # cards showing everything while the table underneath only shows today.
    # Used to default to today specifically; templates/monitoring.html's
    # syncMonitoringFilterUi()/applyMonitoringDateFilter() have their own
    # matching update for what counts as "the default, not an active filter".
    since = request.args.get("since", "")
    until = request.args.get("until", "")

    q = request.args.get("q", "")

    sort_by = request.args.get("sort_by", "detected_at")
    if sort_by not in monitoring_service.MONITORING_SORTABLE_COLUMNS:
        sort_by = "detected_at"
    sort_dir = request.args.get("sort_dir", "desc")
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    return render_template(
        "monitoring.html",
        staff=g.staff,
        **stats,
        limit=limit,
        offset=offset,
        classification=classification,
        since=since,
        until=until,
        q=q,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@pages_bp.route("/audit-log")
@login_required
def audit_log_page():
    """GET - Sidebar's "Activity Log" page shell; /api/v1/audit-log used to
    only return raw JSON before ni. Table loads via htmx same sa
    notifications_page(). Staff filter dropdown admin-only."""
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
    """GET - Approvals queue page shell para sa unrestriction ug deactivation
    requests; rows scoped per role sa downstream service. Tab + per-panel
    status/mine/offset gikan sa URL (u_/d_ prefix) para independent ang
    round-trip sa duha ka tab."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 20, 0

    # tab + each panel's status/mine/offset are read from the URL (u_/d_
    # prefixed so both tabs round-trip independently), so a refresh reopens
    # on the right tab/filters instead of resetting to Unrestriction/Pending.
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


@pages_bp.route("/reservations")
@login_required
@feature_required("reservations")
def reservations_page():
    """GET - Advance-booking list page shell (see
    services/reservation_service.py's module docstring sa feature design).
    Bag-ong reservations gikan ra sa asset's Details modal ("Reserve for
    Later"), dili gikan ani nga page."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        limit, offset = 20, 0
    status = request.args.get("status", "scheduled")
    if status not in ("scheduled", "fulfilled", "cancelled", "expired", ""):
        status = "scheduled"
    return render_template(
        "reservations.html", staff=g.staff, limit=limit, offset=offset,
        status=status, mine=request.args.get("mine") == "1",
    )


@pages_bp.route("/reports")
@roles_required("admin", "in_charge", "tool_keeper")
@feature_required("reports")
def reports_page():
    """GET - page shell ra jud ni; ang mga numbers gikan sa stats.py's JSON
    endpoints, fetched client-side."""

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
    """GET - Barcode label generator page; currently-present assets/members
    ra ang naay labels, print gamit browser's print dialog."""
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
    """GET - Launcher para sa app-wide policy knobs; system.html mo-hide sa
    admin-only cards para sa in_charge. Kada card's live current value
    gi-gather diri para sa iyang summary line, dili na kinahanglan
    individual htmx round-trip per card just to show current value."""
    conn = get_db_connection()
    try:
        loan_limits = {
            row["member_type"]: row["max_concurrent_loans"]
            for row in staff_service.get_loan_limits(conn)
        }
        feature_flags_all = settings_service.get_feature_flags(conn)
        enabled_feature_labels = [
            settings_service.get_feature_label(name)
            for name, enabled in feature_flags_all.items()
            if enabled
        ]
        last_backup_at = backup_service.get_last_backup_at(conn)
        settings_ctx = dict(
            session_idle_minutes=settings_service.get_session_idle_minutes(conn),
            session_absolute_hours=settings_service.get_session_absolute_hours(conn),
            max_failed_attempts=settings_service.get_max_failed_attempts(conn),
            lockout_minutes=settings_service.get_lockout_minutes(conn),
            loan_limit_trainee=loan_limits.get("trainee"),
            loan_limit_trainer=loan_limits.get("trainer"),
            default_loan_days=settings_service.get_default_loan_days(conn),
            checkout_session_stale_minutes=settings_service.get_checkout_session_stale_minutes(conn),
            default_maintenance_interval_days=settings_service.get_default_maintenance_interval_days(conn),
            maintenance_reminder_lead_days=settings_service.get_maintenance_reminder_lead_days(conn),
            damage_restriction_threshold=settings_service.get_damage_restriction_threshold(conn),
            due_soon_hours=settings_service.get_due_soon_hours(conn),
            enabled_feature_labels=enabled_feature_labels,
            auto_backup_enabled=settings_service.get_auto_backup_enabled(conn),
            auto_backup_interval_hours=settings_service.get_auto_backup_interval_hours(conn),
            last_backup_at=last_backup_at.isoformat() if last_backup_at else None,
            archive_purge_label=settings_service.format_archive_purge_label(
                settings_service.get_archive_purge_days(conn)
            ),
            barcode_labels_printed=staff_service.get_barcode_labels_printed_count(conn),
            alarm_enabled=settings_service.get_alarm_enabled(conn),
            asset_id_prefix=settings_service.get_asset_id_prefix(conn),
            member_code_prefix=settings_service.get_member_code_prefix(conn),
            staff_code_prefix=settings_service.get_staff_code_prefix(conn),
        )
    finally:
        conn.close()
    return render_template("system.html", staff=g.staff, **settings_ctx)


@pages_bp.route("/feedback")
@roles_required("admin")
def feedback_page():
    """GET - Admin-only feedback review page shell; list loads via htmx,
    same pattern sa Notifications."""
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
    """GET - ang manual modal na karon; ni nga route naa lang para dili
    mag-404 ang mga old /help bookmarks."""
    return redirect(url_for("home", open_help=1))
