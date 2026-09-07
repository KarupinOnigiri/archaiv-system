from flask import Blueprint, g, render_template, request

from database import get_db_connection
from services import search_service
from services.auth_service import login_required
from services.settings_service import get_feature_flags
from utils.logger import log_action

search_bp = Blueprint("search", __name__)


@search_bp.route("/search")
@login_required
def global_search():
    """Content header's Ctrl/Cmd+/ search bar - one small results dropdown
    covering pages/assets/members/staff (role- and feature-flag-scoped the
    same way each of those tabs already is; see services/search_service.py),
    rendered as an htmx partial so the input's own hx-get can swap it in
    directly."""
    query = request.args.get("q", "")
    conn = get_db_connection()
    try:
        feature_flags = get_feature_flags(conn)
        result = search_service.global_search(conn, query, g.staff, feature_flags)
    finally:
        conn.close()
    # Audit trail for what staff actually searched - only once the query is
    # long enough to be a real search (see search_service.MIN_QUERY_LENGTH),
    # not every keystroke below that or the input's own re-fetch-on-focus
    # with an empty/blank value, which would just spam the log with noise
    # nobody reviewing it would find useful. Fire-and-forget: log_action
    # swallows its own failures (utils/logger.py) so a logging hiccup never
    # breaks the search itself.
    if len(query.strip()) >= search_service.MIN_QUERY_LENGTH:
        log_action(g.staff["id"], "global_search", details=f'Searched for "{query.strip()}"')
    return render_template("_global_search_results.html", **result)
