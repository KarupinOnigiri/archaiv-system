"""RFID exit-monitoring: naay htmx endpoints para sa Monitoring tab, dungan
sa duha ka machine-to-machine endpoints nga gi-call sa Raspberry Pi's RFID
listener (dili part sa repo - see thesis Chapter 3 hardware description)
direkta, walay staff browser session."""

import csv
import hmac
import io
import os
import time
from datetime import datetime

from flask import Blueprint, jsonify, g, request, render_template, make_response
from werkzeug.security import check_password_hash

from database import get_db_connection, get_json_or_error
from services import monitoring_service, monitoring_stats_service, login_service
from services.auth_service import roles_required, feature_required, require_session
from services.settings_service import is_feature_enabled, get_feature_label
from utils.validators import parse_pagination_params
from extensions import limiter

monitoring_bp = Blueprint("monitoring", __name__)

# Roles that can see the Monitoring tab at all - same set as Activity Log.
MONITORING_VIEW_ROLES = ("admin", "in_charge", "tool_keeper")

# Roles allowed to use the software alarm-dismiss override (password
# re-confirmed) when the physical Master Key Card/HF-NFC reader isn't
# available - deliberately narrower than MONITORING_VIEW_ROLES. tool_keeper can see
# the Monitoring tab but not silence a triggered alarm from a browser.
_ALARM_OVERRIDE_ROLES = ("admin", "in_charge")


def _listener_key_error():
    """Shared guard sa duha ka Pi-facing endpoints below. Fail closed ni siya
    - kung wa gi-set ang RFID_LISTENER_KEY, disabled dayon ang duha ka
    endpoints imbes nga tugotan request nga walay secret configured."""
    configured_key = os.environ.get("RFID_LISTENER_KEY")
    if not configured_key:
        return jsonify({"error": "RFID_LISTENER_KEY is not configured on this server"}), 503
    presented_key = request.headers.get("X-RFID-Listener-Key")
    # compare_digest para constant-time ang comparison - parehas sa CSRF
    # token check sa auth_service.py, dili timing side-channel.
    if not presented_key or not hmac.compare_digest(presented_key, configured_key):
        return jsonify({"error": "Invalid or missing listener key"}), 401
    return None


# # # PAGE-SUPPORTING (browser, htmx) ----------------------------------------


def get_monitoring_table_context(args):
    """Helper nga mo-parse sa pagination params ug mo-tawag sa
    monitoring_service.get_monitoring_table_context()."""
    conn = get_db_connection()
    try:
        limit, offset, error = parse_pagination_params(args)
        if error:
            return None, error
        return monitoring_service.get_monitoring_table_context(conn, args, limit, offset)
    finally:
        conn.close()


@monitoring_bp.route("/monitoring/table")
@roles_required(*MONITORING_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
def view_monitoring_table():
    """GET - htmx partial sa Monitoring tab's live event table."""
    context, error = get_monitoring_table_context(request.args)
    if error:
        return error
    return render_template("monitoring/_table.html", **context)


@monitoring_bp.route("/monitoring/<int:id>/details-modal")
@roles_required(*MONITORING_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
def view_event_details_modal(id):
    """GET - read-only details modal para sa usa ka detection event row,
    parehas nga pattern sa transactions/_details_modal.html."""
    conn = get_db_connection()
    try:
        event, error = monitoring_service.get_event_details(conn, id)
    finally:
        conn.close()

    if error:
        return error
    return render_template("monitoring/_details_modal.html", event=event)


@monitoring_bp.route("/monitoring/alarm-banner")
@roles_required(*MONITORING_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
def view_alarm_banner():
    """GET - gi-poll every few seconds sa Monitoring page (see monitoring.html's
    hx-trigger). Delegates to monitoring_service.get_alarm_status(). Passes
    can_override so only admin/in_charge see the "dismiss via password"
    click affordance - tool_keeper still sees the banner, just not clickable."""
    conn = get_db_connection()
    try:
        status = monitoring_service.get_alarm_status(conn)
    finally:
        conn.close()
    return render_template(
        "monitoring/_alarm_banner.html", can_override=g.staff["role"] in _ALARM_OVERRIDE_ROLES, **status
    )


@monitoring_bp.route("/monitoring/alarm-override-modal")
@roles_required(*_ALARM_OVERRIDE_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
def view_alarm_override_modal():
    """GET - password-confirmation modal shell for the alarm banner's
    software-dismiss override. Role-gated same as the POST it submits to -
    defense in depth on top of the banner only rendering the click
    affordance for allowed roles in the first place."""
    return render_template("monitoring/_alarm_override_modal.html")


@monitoring_bp.route("/monitoring/alarm/override-dismiss", methods=["POST"])
@roles_required(*_ALARM_OVERRIDE_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
@limiter.limit("10 per minute; 30 per hour")
def override_dismiss_alarm():
    """POST - fallback alarm-dismiss path for when the physical Master Key
    Card/HF-NFC reader isn't available (broken hardware, pyscard not
    installed, etc.) - NOT the primary mechanism, see
    monitoring_service.deactivate_active_alarms()'s docstring for why one
    still exists deliberately. Requires re-confirming the acting staff
    member's own password (same "prove this session hasn't just been left
    open/hijacked" pattern as staff.py's change_password_submit and
    backup deletion) so this can't be triggered by a bystander at an
    already-unlocked screen. Logged distinctly (via="software_override")
    from a real card tap so the audit trail always shows which path
    actually cleared a given alarm."""
    data, error = get_json_or_error()
    if error:
        return error

    password = data.get("password")
    if not password:
        return jsonify({"error": "Enter your password to confirm this"}), 400

    if not check_password_hash(g.staff["password_hash"], password):
        return jsonify({"error": "Incorrect password"}), 401

    conn = get_db_connection()
    try:
        result, error = monitoring_service.deactivate_active_alarms(
            conn, g.staff["id"], via="software_override"
        )
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@monitoring_bp.route("/monitoring/alarm-status")
@limiter.limit("90 per minute")
def view_alarm_status():
    """GET - deliberately public (walay @login_required) kay ni sila ang
    ga-share ani nga endpoint: static/js/ui.js's global poll (logged-in
    pages), templates/login.html's poll (wa pa naka-login), AND the Pi's
    RFID listener's alarm-relay sync loop (pi_controller/rfid_listener.py,
    ALARM_SYNC_POLL_SECONDS - see its config.py comment). Detail level
    ma-scale depende kinsay ni-request via soft session check
    (require_session, pero gi-ignore ang error) - naay session, full detail;
    walay session, asset name/barcode ra (dili classification/tag/timestamp)
    para dili ma-leak ang incident details sa dili pa naka-log in.
    Feature-flagged pareho sa uban Monitoring endpoints - inactive dayon ang
    report kung off.

    90/min, not the usual tighter limits elsewhere on this file - this one
    is polled automatically and continuously by every open tab (every ~5s
    each, per ui.js/login.html), not triggered by a single user action, so
    its real per-client volume scales with how many tabs/devices one person
    has open rather than with intent; a couple of tabs plus the Pi
    controller's own steady ~20/min shouldn't be able to trip this by
    accident (see TRUSTED_PROXY_HOPS in app.py/.env.example for why each
    real client is tracked separately in the first place, once behind a
    reverse proxy)."""
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
@roles_required(*MONITORING_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
def view_monitoring_stat_cards():
    """GET - htmx partial sa Monitoring tab's stat cards. Delegates to
    monitoring_service.get_monitoring_stat_cards()."""
    conn = get_db_connection()
    try:
        stats = monitoring_service.get_monitoring_stat_cards(conn)
    finally:
        conn.close()
    return render_template("monitoring/_stat_cards.html", **stats)


@monitoring_bp.route("/monitoring/stat-cards/counts")
@roles_required(*MONITORING_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
def view_monitoring_stat_counts():
    """GET - numbers-only JSON version of the stat cards, for the 1s
    auto-poll in monitoring.html. Deliberately NOT the HTML partial above:
    swapping that partial's markup on every poll tick wipes and reapplies
    the JS-driven highlight classes (ring-2/stat-card-active/etc.) on the
    currently-selected card, and since those classes carry a CSS transition,
    every swap visibly re-animates the glow even when nothing changed - the
    exact flicker bug fixed for the table's own poll. This just hands back
    the four numbers so the client can update each card's count text
    in place, never touching the DOM nodes or their classes at all."""
    conn = get_db_connection()
    try:
        stats = monitoring_service.get_monitoring_stat_cards(conn)
    finally:
        conn.close()
    return jsonify(stats)


# # # TRENDS (historical aggregation - not the live/today table above) -----


@monitoring_bp.route("/monitoring/trends")
@roles_required(*MONITORING_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
@limiter.limit("20 per minute")
def view_monitoring_trends():
    """GET - data para sa Trends tab: daily volume by classification,
    hour-of-day histogram, alarm response-time stats, ug unknown_tag-rate
    proxy for false-positive rate (see monitoring_stats_service's module
    docstring for limitations). Same since/until/classification/q filters
    sa live table (build_monitoring_filters)."""
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
        rfid_response_timing, error = monitoring_stats_service.get_rfid_response_timing(conn, request.args)
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
            "rfid_response_timing": rfid_response_timing,
        }
    )


@monitoring_bp.route("/monitoring/trends/rfid-response-timing.csv")
@roles_required(*MONITORING_VIEW_ROLES)
@feature_required("monitoring", redirect_to_dashboard=False)
@limiter.limit("10 per minute")
def export_rfid_response_timing():
    """GET - raw per-event CSV for RFID response-time data (RFID
    acknowledgment response time, alarm activation response time) - same
    since/until/classification/q filters as the rest of Trends, but
    uncapped (row_limit=None) unlike the in-page chart/table, which only
    needs the most recent slice - an export should include everything in
    the requested range, not just what's convenient to chart."""
    conn = get_db_connection()
    try:
        result, error = monitoring_stats_service.get_rfid_response_timing(conn, request.args, row_limit=None)
    finally:
        conn.close()
    if error:
        return error

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Event ID", "Detected At", "RFID Tag", "Classification", "Alarm Activated",
            "RFID Acknowledgment Response Time (ms)", "Alarm Activation Response Time (ms)",
        ]
    )
    for e in result["events"]:
        writer.writerow(
            [
                e["id"], e["detected_at"], e["rfid_tag_scanned"], e["classification"],
                "Yes" if e["alarm_activated"] else "No",
                e["ack_response_ms"] if e["ack_response_ms"] is not None else "",
                e["alarm_activation_ms"] if e["alarm_activation_ms"] is not None else "",
            ]
        )

    response = make_response(buf.getvalue().encode("utf-8-sig"))  # BOM so Excel opens UTF-8 CSVs correctly
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    response.headers["Content-Disposition"] = f"attachment; filename=rfid_response_timing_{timestamp}.csv"
    return response


# # # MACHINE-TO-MACHINE (Raspberry Pi RFID listener, no staff session) -----


@monitoring_bp.route("/rfid-events", methods=["POST"])
@limiter.limit("60 per minute")
def record_rfid_event():
    """POST - gi-call sa Pi's background RFID listener per de-duplicated
    detection sa exit gate. Auth kay shared secret (RFID_LISTENER_KEY), dili
    staff login, kay headless man ni nga walay browser session. See
    monitoring_service.record_rfid_event() sa classification rule.

    Rate-limited pareho sa uban sensitive endpoints - kung ma-leak ra gyud
    ang RFID_LISTENER_KEY, wala unta'y throttle nga nag-guard sa write
    volume diri gawas sa app-wide 200/min default. 60/min comfortably
    covers real exit-gate bursts (multiple tagged items in one crossing)
    without being loose enough to matter for abuse.

    server_processed_at_ms (migration 0061) is
    captured HERE, at request entry, before the key check/feature check/DB
    work below - not inside monitoring_service.record_rfid_event() itself -
    so the "RFID acknowledgment response time" this pairs with
    client_detected_at_ms to produce reflects when the request actually
    arrived, not when this function happened to finish its own unrelated
    work first."""
    server_processed_at_ms = int(time.time() * 1000)

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
        result, error = monitoring_service.record_rfid_event(
            conn,
            data.get("rfid_tag_scanned"),
            client_detected_at_ms=data.get("client_detected_at_ms"),
            server_processed_at_ms=server_processed_at_ms,
        )
    finally:
        conn.close()

    if error:
        return error
    return jsonify(result)


@monitoring_bp.route("/rfid-events/<int:event_id>/alarm-timing", methods=["POST"])
@limiter.limit("60 per minute")
def record_alarm_relay_timing(event_id):
    """POST - follow-up call from the Pi's listener reporting the instant
    gpio_relay.activate() actually energized the relay for an alarm-
    activated event (pi_controller/rfid_listener.py, right after that call
    succeeds). Necessarily separate from record_rfid_event() above - the
    relay only gets activated on the Pi AFTER this app has already
    responded with alarm_activated=True, so there's no way to have this
    timestamp at insert time. Same shared-secret M2M auth as the other two
    listener-facing endpoints. See monitoring_service.record_alarm_relay_
    timing() for why a not-found/no-op result isn't treated as an error."""
    key_error = _listener_key_error()
    if key_error:
        return key_error

    data, error = get_json_or_error()
    if error:
        return error

    alarm_relay_activated_at_ms = data.get("alarm_relay_activated_at_ms")
    if not isinstance(alarm_relay_activated_at_ms, int):
        return jsonify({"error": "Missing/invalid required field: alarm_relay_activated_at_ms"}), 400

    conn = get_db_connection()
    try:
        updated = monitoring_service.record_alarm_relay_timing(conn, event_id, alarm_relay_activated_at_ms)
    finally:
        conn.close()

    return jsonify({"updated": updated})


@monitoring_bp.route("/rfid-events/deactivate", methods=["POST"])
@limiter.limit("10 per minute")
def deactivate_rfid_alarm():
    """POST - gi-call sa Pi's HF/NFC handler kung mo-tap ang Admin sa Master
    Key Card sa tool room - mao ra ni nga paagi para ma-clear ang active
    alarm. Identifies the Admin same way sa workstation RFID login
    (staff.rfid_id, role='admin'); walay software-only "deactivate" button
    sa browser UI, by design.

    Tighter than record_rfid_event's 60/min - a real tap only ever
    happens as one deliberate admin action, not a burst, so this matches
    the same "single deliberate action" limit login() itself uses."""
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
