from flask import Blueprint, render_template, request

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/login")
def login_page():
    error = request.args.get("error")
    return render_template("login.html", error=error)
