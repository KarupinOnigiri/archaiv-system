"""Watches the HF/NFC Reader/Writer (ACR122U) for a Master Key Card tap,
used exclusively for alarm deactivation - this is the "deliberate tap
requiring physical presence at the site of the alert" described in the
thesis, distinct from the workstation's own ACR122U used for Admin
login/tag-provisioning.

Uses pyscard (PC/SC), the standard-compliant interface the ACR122U's own
Application Programming Interface guide documents (its APDUs are PC/SC
APDUs, e.g. "Get Data" for the card UID - see that document's section 4.1).
Requires pcscd running on the Pi with the CCID driver active; no ACS-specific
driver is needed (the ACR122U enumerates as a standard USB CCID device).
"""

import logging
import time

from . import config

logger = logging.getLogger(__name__)

# PC/SC "Get Data" APDU for the card serial number/UID - FF CA 00 00 00h,
# per the ACR122U Application Programming Interface guide, section 4.1.
_GET_UID_APDU = [0xFF, 0xCA, 0x00, 0x00, 0x00]


def _uid_to_string(data):
    """Formats the UID bytes the same way a keyboard-wedge reader would --
    uppercase hex, no separators - so it matches whatever format
    staff.rfid_id was populated with via the workstation reader."""
    return "".join(f"{b:02X}" for b in data)


def watch_for_taps(on_tap):
    """Blocks forever, calling on_tap(uid_string) once per card presented.
    Reconnects automatically on reader errors instead of dying the whole
    listener process."""
    if config.DRY_RUN:
        logger.info("[DRY RUN] master_key_reader active - no real reader will be opened.")
        while True:
            time.sleep(3600)

    try:
        from smartcard.System import readers
        from smartcard.Exceptions import NoCardException, CardConnectionException
    except ImportError as e:
        # PC/SC itself is cross-platform (native on Windows via the Smart
        # Card service, pcscd on Linux) - this only fires if pyscard isn't
        # installed at all on this machine. Idling instead of crashing keeps
        # this consistent with hid_tag_reader.py/gpio_relay.py's own
        # graceful-degradation story: a machine missing one optional
        # peripheral (no Master Key Card reader attached, say) shouldn't
        # take down the antenna-reading thread running fine alongside it.
        logger.error(f"pyscard isn't available on this machine ({e}) - master key card taps won't be detected.")
        while True:
            time.sleep(3600)

    last_uid = None
    last_seen = 0.0
    # Same-card re-tap suppression window - a card left resting on the
    # reader would otherwise fire on_tap() repeatedly, same problem as the
    # UHF antenna's own continuous-read behavior.
    _RETAP_COOLDOWN_SECONDS = 2.0

    while True:
        try:
            reader_list = readers()
            if not reader_list:
                logger.warning("No PC/SC readers found; retrying in 3s...")
                time.sleep(3)
                continue

            reader = reader_list[0]
            if config.PCSC_READER_NAME:
                matching = [r for r in reader_list if config.PCSC_READER_NAME.lower() in str(r).lower()]
                if matching:
                    reader = matching[0]
                else:
                    logger.warning(
                        f"No PC/SC reader matched PCSC_READER_NAME='{config.PCSC_READER_NAME}'; "
                        f"using '{reader}' instead."
                    )

            connection = reader.createConnection()
            try:
                connection.connect()
            except (NoCardException, CardConnectionException):
                time.sleep(0.5)
                continue

            data, sw1, sw2 = connection.transmit(_GET_UID_APDU)
            if (sw1, sw2) != (0x90, 0x00):
                logger.warning(f"Get Data failed (SW={sw1:02X}{sw2:02X}); card may not be readable.")
                time.sleep(0.5)
                continue

            uid = _uid_to_string(data)
            now = time.monotonic()
            if uid == last_uid and (now - last_seen) < _RETAP_COOLDOWN_SECONDS:
                time.sleep(0.3)
                continue
            last_uid, last_seen = uid, now

            on_tap(uid)
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"Master key reader error: {e}; retrying in 3s...")
            time.sleep(3)
