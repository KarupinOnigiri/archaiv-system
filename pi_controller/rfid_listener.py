"""Entry point for the tool-room headless controller.

Runs two background loops for the lifetime of the process:
  1. dual_reader.read_tags() - the fixed UHF RFID Reader Antenna at the
     monitored exit, over whichever of USB or network it's actually wired to
     (auto-detected; see dual_reader.py). Each de-duplicated tag read is
     POSTed to the web app's /api/v1/rfid-events; if the app classifies it as
     unauthorized (or an unknown/unregistered tag), the alarm relay is
     energized immediately.
  2. master_key_reader.watch_for_taps() - the HF/NFC Reader/Writer. Each
     card tap is POSTed to /api/v1/rfid-events/deactivate; only if the app
     confirms the card belongs to an admin AND an alarm was actually active
     does the relay get de-energized.

The web app is the single source of truth for classification and alarm
state (services/monitoring_service.py) - this script never decides
authorized/unauthorized on its own, it only reports raw detections and
raw taps, and acts on what the app tells it. That keeps the audit trail,
the confusion-matrix data, and the actual alarm behavior all coming from
one place instead of two systems that could disagree.

Run with:
    python -m pi_controller.rfid_listener
(from the project root, with pi_controller/.env configured - see
pi_controller/.env.example)
"""

import logging
import sys
import threading
import time

from . import api_client, config, dual_reader, gpio_relay, master_key_reader

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pi_controller")

# Tracks the de-dup cooldown per tag locally, in addition to whatever the
# reader itself is configured to suppress - belt-and-suspenders, since a
# reconfigured reader (or a different unit) shouldn't be able to silently
# turn this into a flood of duplicate API calls.
_last_seen = {}
_last_seen_lock = threading.Lock()


def _handle_tag_detected(tag_id):
    now = time.monotonic()
    with _last_seen_lock:
        last = _last_seen.get(tag_id)
        if last is not None and (now - last) < config.DEDUP_COOLDOWN_SECONDS:
            return
        _last_seen[tag_id] = now

    logger.info(f"Tag detected: {tag_id}")
    result = api_client.record_detection(tag_id)
    if result is None:
        return

    classification = result.get("classification")
    if result.get("alarm_activated"):
        logger.warning(f"Tag '{tag_id}' classified {classification} - activating alarm.")
        gpio_relay.activate()
    else:
        logger.info(f"Tag '{tag_id}' classified {classification} - no alarm action.")


def _handle_master_key_tap(uid):
    logger.info(f"Master Key Card tapped: {uid}")
    result = api_client.deactivate_alarm(uid)
    if result is None:
        return

    count = result.get("deactivated_count", 0)
    if count > 0:
        logger.info(f"Cleared {count} active alarm event(s) - de-energizing relay.")
        gpio_relay.deactivate()
    else:
        logger.info("Card accepted, but no alarm was active - nothing to clear.")


def main():
    if not config.RFID_LISTENER_KEY and not config.DRY_RUN:
        logger.error("RFID_LISTENER_KEY is not set. Configure pi_controller/.env before running for real.")
        sys.exit(1)

    logger.info(f"Starting tool-room controller (app: {config.APP_BASE_URL}, dry_run={config.DRY_RUN})")

    tag_thread = threading.Thread(
        target=dual_reader.read_tags, args=(_handle_tag_detected,), name="uhf-reader", daemon=True
    )
    key_thread = threading.Thread(
        target=master_key_reader.watch_for_taps, args=(_handle_master_key_tap,), name="master-key-reader", daemon=True
    )

    tag_thread.start()
    key_thread.start()

    try:
        while True:
            time.sleep(1)
            if not tag_thread.is_alive() or not key_thread.is_alive():
                logger.error("A reader thread has died unexpectedly - exiting so a service manager can restart us.")
                sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        gpio_relay.cleanup()


if __name__ == "__main__":
    main()
