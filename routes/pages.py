from flask import Blueprint, render_template, request, g
from database import get_db_connection
from services.auth_service import login_required
from routes.assets import get_assets_table_context  # adjust the module path here to match wherever assets.py actually lives in your project (you mentioned routes/assets1.py earlier -- if so, use `from routes.assets1 import get_assets_table_context`)
from routes.members import get_members_table_context  # same note as above -- adjust to match wherever members.py actually lives
from routes.staff import get_staff_table_context  # same note as above -- adjust to match wherever staff.py actually lives
from routes.transactions import get_transactions_table_context  # same note as above -- adjust to match wherever transactions.py actually lives

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/login")
def login_page():
    error = request.args.get("error")
    username = request.args.get("username", "")
    return render_template("login.html", error=error, username=username)


@pages_bp.route("/assets")
@login_required
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
@login_required
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
        **table_context,
    )
