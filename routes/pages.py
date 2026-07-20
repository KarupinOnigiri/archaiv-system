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
    limit = DEFAULT_LIMIT
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
