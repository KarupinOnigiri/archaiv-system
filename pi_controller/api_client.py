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


def record_detection(rfid_tag_scanned):
    """POSTs one de-duplicated detection event. Returns the parsed JSON body
    on success (containing at minimum 'classification' and 'alarm_activated'),
    or None on any failure - network errors, timeouts, and non-2xx
    responses are all logged and treated the same way by the caller: skip
    this event, keep the listener running."""
    if not config.RFID_LISTENER_KEY:
        logger.error("RFID_LISTENER_KEY is not set - refusing to call the API. See .env.example.")
        return None

    url = f"{config.APP_BASE_URL}/api/v1/rfid-events"
    try:
        response = requests.post(
            url,
            json={"rfid_tag_scanned": rfid_tag_scanned},
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
