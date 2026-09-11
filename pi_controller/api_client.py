"""Thin wrapper around the two machine-to-machine endpoints the web app
exposes for this controller (routes/monitoring.py in the main app). Kept
separate from the reader/relay modules so retry/timeout/logging behavior
lives in exactly one place.
"""

import logging

import requests

from . import config

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5
_HEADERS = {"Content-Type": "application/json"}


def _headers():
    headers = dict(_HEADERS)
    if config.RFID_LISTENER_KEY:
        headers["X-RFID-Listener-Key"] = config.RFID_LISTENER_KEY
    return headers


def record_detection(rfid_tag_scanned, client_detected_at_ms=None):
    """POSTs one de-duplicated detection event. Returns the parsed JSON body
    on success (containing at minimum 'id', 'classification' and
    'alarm_activated'), or None on any failure - network errors, timeouts,
    and non-2xx responses are all logged and treated the same way by the
    caller: skip this event, keep the listener running.

    client_detected_at_ms (response-time measurement, migration 0061 on the
    app side) - epoch-millisecond int, the instant the antenna reported this
    tag (captured by the caller, rfid_listener.py's _handle_tag_detected,
    before this function's own network call adds latency). Optional -
    omitting it just leaves that column NULL on the app side, same as
    before this was added."""
    if not config.RFID_LISTENER_KEY:
        logger.error("RFID_LISTENER_KEY is not set - refusing to call the API. See .env.example.")
        return None

    url = f"{config.APP_BASE_URL}/api/v1/rfid-events"
    payload = {"rfid_tag_scanned": rfid_tag_scanned}
    if client_detected_at_ms is not None:
        payload["client_detected_at_ms"] = client_detected_at_ms
    try:
        response = requests.post(
            url,
            json=payload,
            headers=_headers(),
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        logger.error(f"Failed to reach {url}: {e}")
        return None

    if response.status_code != 200:
        logger.error(f"Recording detection for '{rfid_tag_scanned}' failed ({response.status_code}): {response.text}")
        return None

    return response.json()


def deactivate_alarm(rfid_id):
    """POSTs a Master Key Card tap. Returns the parsed JSON body
    ({'deactivated_count': N}) on success, or None on failure."""
    if not config.RFID_LISTENER_KEY:
        logger.error("RFID_LISTENER_KEY is not set - refusing to call the API. See .env.example.")
        return None

    url = f"{config.APP_BASE_URL}/api/v1/rfid-events/deactivate"
    try:
        response = requests.post(
            url,
            json={"rfid_id": rfid_id},
            headers=_headers(),
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        logger.error(f"Failed to reach {url}: {e}")
        return None

    if response.status_code == 404:
        logger.warning(f"Card tapped ({rfid_id}) is not registered to an admin account - alarm left active.")
        return None

    if response.status_code != 200:
        logger.error(f"Deactivating alarm for card '{rfid_id}' failed ({response.status_code}): {response.text}")
        return None

    return response.json()


def report_alarm_relay_timing(event_id, alarm_relay_activated_at_ms):
    """POSTs the instant gpio_relay.activate() actually energized the relay
    for one alarm-activated event (the "alarm activation response time"
    measurement). Necessarily a separate call from record_detection()
    above - the relay only gets activated AFTER that call already returned
    with alarm_activated=True, so this timestamp can't exist yet at that
    point. Best-effort: logs and returns on any failure same as the other
    calls here, but a missed timing report doesn't affect the alarm itself
    (already activated) or need a retry - it just means that one trial's
    activation-response-time is missing, not the alarm handling."""
    if not config.RFID_LISTENER_KEY:
        logger.error("RFID_LISTENER_KEY is not set - refusing to call the API. See .env.example.")
        return None

    url = f"{config.APP_BASE_URL}/api/v1/rfid-events/{event_id}/alarm-timing"
    try:
        response = requests.post(
            url,
            json={"alarm_relay_activated_at_ms": alarm_relay_activated_at_ms},
            headers=_headers(),
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        logger.error(f"Failed to reach {url}: {e}")
        return None

    if response.status_code != 200:
        logger.error(f"Reporting alarm-relay timing for event {event_id} failed ({response.status_code}): {response.text}")
        return None

    return response.json()


def get_alarm_status():
    """GETs the app's public alarm-status endpoint (no auth needed - see
    routes/monitoring.py's view_alarm_status). Returns True/False for
    whether an alarm is currently active app-side, or None on any failure
    (network error, timeout, non-2xx) so the caller can just skip that poll
    tick rather than guess. Used by rfid_listener.py's sync loop to keep the
    physical relay matching the app's alarm state even when it was cleared
    by the software password-override rather than a Master Key Card tap."""
    url = f"{config.APP_BASE_URL}/api/v1/monitoring/alarm-status"
    try:
        response = requests.get(url, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        logger.error(f"Failed to reach {url}: {e}")
        return None

    if response.status_code != 200:
        logger.error(f"Polling alarm status failed ({response.status_code}): {response.text}")
        return None

    return response.json().get("active")
