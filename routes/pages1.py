from flask import Blueprint, render_template, request, g
from services.auth_service import login_required
from database import get_db_connection

pages_bp = Blueprint("pages", __name__)

DEFAULT_LIMIT = 20


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

    # Same filters _table.html's own pagination links already expect
    # (q, show_archived) so the initial render and subsequent htmx swaps
    # stay consistent with each other.
    show_archived = request.args.get("show_archived") == "1"
    q = request.args.get("q", "").strip()
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except ValueError:
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0

    where = ["deleted_at IS NOT NULL" if show_archived else "deleted_at IS NULL"]
    params = []
    if q:
        where.append("(name LIKE ? OR asset_id LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where_clause = " AND ".join(where)

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM assets WHERE {where_clause}", params
    ).fetchone()["cnt"]

    assets = conn.execute(
        f"""SELECT assets.*, categories.name AS category
            FROM assets
            LEFT JOIN categories ON categories.id = assets.category_id
            WHERE {where_clause}
            ORDER BY assets.created_at DESC LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()

    conn.close()

    return render_template(
        "assets/index.html",
        staff=g.staff,
        stat_all=stat_all,
        stat_available=stat_available,
        stat_borrowed=stat_borrowed,
        stat_maintenance=stat_maintenance,
        assets=assets,
        show_archived=show_archived,
        limit=limit,
        offset=offset,
        total_count=total_count,
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

    departments = conn.execute("SELECT * FROM departments ORDER BY name").fetchall()

    show_inactive = request.args.get("show_inactive") == "1"
    q = request.args.get("q", "").strip()
    department_id = request.args.get("department_id")
    is_restricted = request.args.get("is_restricted")
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except ValueError:
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0

    where = [] if show_inactive else ["members.is_active = 1"]
    params = []
    if q:
        where.append(
            "(members.first_name LIKE ? OR members.last_name LIKE ? OR members.barcode_id LIKE ? OR members.member_code LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if department_id:
        where.append("members.department_id = ?")
        params.append(department_id)
    if is_restricted in ("0", "1"):
        where.append("members.is_restricted = ?")
        params.append(int(is_restricted))
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM members {where_clause}", params
    ).fetchone()["cnt"]

    members = conn.execute(
        f"""SELECT members.*, departments.name AS department
            FROM members
            JOIN departments ON members.department_id = departments.id
            {where_clause}
            ORDER BY members.id LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()

    conn.close()

    return render_template(
        "members.html",
        staff=g.staff,
        stat_all=stat_all,
        stat_active=stat_active,
        stat_restricted=stat_restricted,
        stat_inactive=stat_inactive,
        departments=departments,
        members=members,
        show_inactive=show_inactive,
        limit=limit,
        offset=offset,
        total_count=total_count,
    )
