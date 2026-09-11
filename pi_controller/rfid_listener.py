"""Entry point for the tool-room headless controller.

Runs three background loops for the lifetime of the process:
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
  3. _sync_alarm_relay() - polls the app's public /api/v1/monitoring/
     alarm-status endpoint every config.ALARM_SYNC_POLL_SECONDS and keeps
     the relay matching whatever the app currently thinks the alarm state
     is. Needed because the admin/in_charge "dismiss via password" fallback
     in the app (routes/monitoring.py's override_dismiss_alarm(), for when
     this reader itself is broken) only clears the app-side record on its
     own - without this loop the physical lamp/buzzer would keep sounding
     even after that dismissal. Loop 2 above still reacts to a real card tap
     immediately rather than waiting out the poll interval.

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
    # Captured before the dedup check/lock below - the "RFID acknowledgment
    # response time" measurement pairs this with the app's own
    # server_processed_at_ms (routes/monitoring.py, captured at that
    # request's entry) to measure the gap, so this needs to be the instant
    # the reader actually reported the tag, not the instant this function
    # happens to get around to calling the API further down.
    client_detected_at_ms = int(time.time() * 1000)

    now = time.monotonic()
    with _last_seen_lock:
        last = _last_seen.get(tag_id)
        if last is not None and (now - last) < config.DEDUP_COOLDOWN_SECONDS:
            return
        _last_seen[tag_id] = now

    logger.info(f"Tag detected: {tag_id}")
    result = api_client.record_detection(tag_id, client_detected_at_ms=client_detected_at_ms)
    if result is None:
        return

    classification = result.get("classification")
    if result.get("alarm_activated"):
        logger.warning(f"Tag '{tag_id}' classified {classification} - activating alarm.")
        gpio_relay.activate()
        # Reported as a separate follow-up call, not part of the detection
        # response above - the relay can only be activated AFTER the app
        # already told us to, so this timestamp doesn't exist until now.
        # Best-effort: a failed report here doesn't undo the alarm or need
        # a retry, it just means this one trial's activation-response-time
        # data point is missing (see api_client's docstring).
        event_id = result.get("id")
        if event_id is not None:
            api_client.report_alarm_relay_timing(event_id, int(time.time() * 1000))
    else:
        logger.info(f"Tag '{tag_id}' classified {classification} - no alarm action.")


def _sync_alarm_relay():
    """Runs for the lifetime of the process, polling the app's alarm-status
    endpoint and keeping the relay's physical state matching it. Tracks the
    last-known state locally purely to avoid spamming redundant on()/off()
    calls (and their log lines) every poll tick - gpio_relay itself is
    idempotent either way, so this is an optimization, not a correctness
    requirement. A failed poll (network hiccup, app briefly down) just logs
    via api_client and is retried next tick; it never guesses and never
    touches the relay on a failed/ambiguous read."""
    last_known_active = None
    while True:
        time.sleep(config.ALARM_SYNC_POLL_SECONDS)
        active = api_client.get_alarm_status()
        if active is None:
            continue  # poll failed - leave the relay as-is, try again next tick

        if active != last_known_active:
            if active:
                logger.info("Alarm-status sync: app reports an alarm is active - energizing relay.")
                gpio_relay.activate()
            else:
                logger.info("Alarm-status sync: app reports no active alarm - de-energizing relay.")
                gpio_relay.deactivate()
            last_known_active = active


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
    sync_thread = threading.Thread(target=_sync_alarm_relay, name="alarm-relay-sync", daemon=True)

    tag_thread.start()
    key_thread.start()
    sync_thread.start()

    try:
        while True:
            time.sleep(1)
            if not tag_thread.is_alive() or not key_thread.is_alive() or not sync_thread.is_alive():
                logger.error("A background thread has died unexpectedly - exiting so a service manager can restart us.")
                sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        gpio_relay.cleanup()


if __name__ == "__main__":
    main()
