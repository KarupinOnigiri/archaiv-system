from flask import Blueprint, render_template, request, g
from services.auth_service import login_required
from database import get_db_connection

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
    conn.close()
    return render_template(
        "assets/index.html",
        staff=g.staff,
        stat_all=stat_all,
        stat_available=stat_available,
        stat_borrowed=stat_borrowed,
    )

