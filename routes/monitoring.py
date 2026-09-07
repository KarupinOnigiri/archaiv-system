"""RFID exit-monitoring: the Monitoring tab's htmx endpoints, plus the two
machine-to-machine endpoints the Raspberry Pi's RFID listener (not part of
this repo - see the thesis's Chapter 3 hardware description) is expected to
call directly, without a staff browser session.
"""

import os

from flask import Blueprint, jsonify, g, request, render_template

from database import get_db_connection, get_json_or_error
from services import monitoring_service, monitoring_stats_service, login_service
from services.auth_service import roles_required, feature_required, require_session
from services.settings_service import is_feature_enabled, get_feature_label
from utils.validators import parse_pagination_params
from extensions import limiter

monitoring_bp = Blueprint("monitoring", __name__)

# Roles that can see the Monitoring tab at all - same set as Activity Log.
_VIEW_ROLES = ("admin", "in_charge", "tool_keeper")


def _listener_key_error():
    """Shared guard for the two Pi-facing endpoints below. Fails closed:
    an unset RFID_LISTENER_KEY disables both endpoints entirely rather than
    accepting requests with no secret configured."""
    configured_key = os.environ.get("RFID_LISTENER_KEY")
    if not configured_key:
        return jsonify({"error": "RFID_LISTENER_KEY is not configured on this server"}), 503
    presented_key = request.headers.get("X-RFID-Listener-Key")
    if not presented_key or presented_key != configured_key:
        return jsonify({"error": "Invalid or missing listener key"}), 401
    return None


# # # PAGE-SUPPORTING (browser, htmx) ----------------------------------------


def get_monitoring_table_context(args):
    conn = get_db_connection()
    try:
        limit, offset, error = parse_pagination_params(args)
        if error:
            return None, error
        return monitoring_service.get_monitoring_table_context(conn, args, limit, offset)
    finally:
        conn.close()


@monitoring_bp.route("/monitoring/table")
@roles_required(*_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
def view_monitoring_table():
    context, error = get_monitoring_table_context(request.args)
    if error:
        return error
    return render_template("monitoring/_table.html", **context)


@monitoring_bp.route("/monitoring/alarm-banner")
@roles_required(*_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
def view_alarm_banner():
    """Polled every few seconds by the Monitoring page - see monitoring.html's hx-trigger."""
    conn = get_db_connection()
    try:
        status = monitoring_service.get_alarm_status(conn)
    finally:
        conn.close()
    return render_template("monitoring/_alarm_banner.html", **status)


@monitoring_bp.route("/monitoring/alarm-status")
@limiter.limit("30 per minute")
def view_alarm_status():
    """Deliberately public (no @login_required) - this is what lets
    static/js/ui.js's global poll (every logged-in page) AND templates/
    login.html's poll (nobody logged in yet) both work off the one
    endpoint. Detail is scaled to who's asking, via a soft session check
    (require_session, but its error is discarded rather than returned):
    a valid staff session gets the same detail the Monitoring page itself
    shows; no session at all (only reachable from the login page, since
    every other page requires one) gets just the asset's name/barcode,
    deliberately not the classification/raw tag/timestamp a logged-in
    viewer sees - enough to grab attention and send someone to log in and
    look, without broadcasting incident specifics to anyone who can merely
    reach the login screen on the network. Feature-flagged like every
    other Monitoring endpoint - reports inactive outright if it's off."""
    if not is_feature_enabled("monitoring"):
        return jsonify({"active": False})

    conn = get_db_connection()
    try:
        status = monitoring_service.get_alarm_status(conn)
        staff, _ = require_session(conn)
    finally:
        conn.close()

    if not status["active"]:
        return jsonify({"active": False})

    event = status["event"] or {}
    payload = {
        "active": True,
        "active_count": status["active_count"],
        "asset_name": event.get("asset_name"),
        "asset_barcode": event.get("asset_barcode"),
    }
    if staff:
        payload["classification"] = event.get("classification")
        payload["rfid_tag_scanned"] = event.get("rfid_tag_scanned")
        payload["detected_at"] = event.get("detected_at")
    return jsonify(payload)


@monitoring_bp.route("/monitoring/stat-cards")
@roles_required(*_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
def view_monitoring_stat_cards():
    conn = get_db_connection()
    try:
        stats = monitoring_service.get_monitoring_stat_cards(conn)
    finally:
        conn.close()
    return render_template("monitoring/_stat_cards.html", **stats)


# # # TRENDS (historical aggregation - not the live/today table above) -----


@monitoring_bp.route("/monitoring/trends")
@roles_required(*_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
@limiter.limit("20 per minute")
def view_monitoring_trends():
    """Backs the Trends tab: daily volume by classification, hour-of-day
    histogram, alarm response-time stats, and the unknown_tag-rate proxy for
    false-positive rate - see monitoring_stats_service's module docstring for
    what this data can't answer yet (per-exit-gate breakdown, true
    false-positive flagging). Accepts the same since/until/classification/q
    filters as the live table (build_monitoring_filters), so the Trends tab
    can reuse the page's existing date-filter UI."""
    conn = get_db_connection()
    try:
        daily_volume, error = monitoring_stats_service.get_daily_event_volume(conn, request.args)
        if error:
            return error
        hourly_distribution, error = monitoring_stats_service.get_hourly_distribution(conn, request.args)
        if error:
            return error
        alarm_response, error = monitoring_stats_service.get_alarm_response_times(conn, request.args)
        if error:
            return error
        unknown_tag_rate, error = monitoring_stats_service.get_unknown_tag_rate_trend(conn, request.args)
        if error:
            return error
    finally:
        conn.close()

    return jsonify(
        {
            "daily_volume": daily_volume,
            "hourly_distribution": hourly_distribution,
            "alarm_response": alarm_response,
            "unknown_tag_rate": unknown_tag_rate,
        }
    )


# # # MACHINE-TO-MACHINE (Raspberry Pi RFID listener, no staff session) -----


@monitoring_bp.route("/rfid-events", methods=["POST"])
def record_rfid_event():
    """Called by the Pi's background RFID listener for each de-duplicated
    detection at the monitored exit. Authenticated by a shared secret
    (RFID_LISTENER_KEY), not a staff login - this runs headless with no
    browser session behind it. See monitoring_service.record_rfid_event for
    the classification rule."""
    key_error = _listener_key_error()
    if key_error:
        return key_error

    if not is_feature_enabled("monitoring"):
        return jsonify({"error": f"{get_feature_label('monitoring')} is currently disabled"}), 403

    data, error = get_json_or_error()
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = monitoring_service.record_rfid_event(conn, data.get("rfid_tag_scanned"))
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@monitoring_bp.route("/rfid-events/deactivate", methods=["POST"])
def deactivate_rfid_alarm():
    """Called by the Pi's HF/NFC handler when the Admin taps the Master Key
    Card at the tool room - the only way an active alarm clears. Identifies
    the Admin the same way workstation RFID login does (staff.rfid_id,
    role='admin'); there is no equivalent software-only "deactivate" action
    anywhere in the browser UI, by design."""
    key_error = _listener_key_error()
    if key_error:
        return key_error

    if not is_feature_enabled("monitoring"):
        return jsonify({"error": f"{get_feature_label('monitoring')} is currently disabled"}), 403

    data, error = get_json_or_error()
    if error:
        return error

    rfid_id = (data.get("rfid_id") or "").strip()
    if not rfid_id:
        return jsonify({"error": "Missing required field: rfid_id"}), 400

    conn = get_db_connection()
    try:
        admin = login_service.find_admin_for_rfid_login(conn, rfid_id)
        if admin is None:
            return jsonify({"error": "Card is not registered to an admin account"}), 404
        result, error = monitoring_service.deactivate_active_alarms(conn, admin["id"])
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)
