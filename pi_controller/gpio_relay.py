"""Drives the 2-channel relay module wired to the Pi's GPIO, which switches
the 12V warning lamp/buzzer (see the thesis's Bill of Materials/hardware
setup). Uses gpiozero rather than raw RPi.GPIO - simpler API, and it
already has a clean pin-factory abstraction that makes DRY_RUN trivial.

This module owns the alarm's physical ON/OFF state. It does NOT decide
*when* to activate/deactivate - rfid_listener.py calls activate()/
deactivate() based on what the web app's API told it (alarm_activated on a
detection, a successful deactivate_alarm() call on a Master Key Card tap,
or the periodic alarm-status sync loop noticing the app-side state was
cleared some other way, e.g. the admin/in_charge password override).

Runs on a Windows PC too, for everything except the alarm relay itself --
there's no GPIO header on a regular PC, hardware or software can't change
that. Rather than crash the whole listener process the moment the relay is
first needed, this degrades to a loud log line: alarm state still gets
recorded correctly on the web app's side (rfid_events / audit trail), the
physical lamp/buzzer just doesn't fire because there's nothing to fire on
this machine. DRY_RUN is a different, deliberate thing (developer testing --
skip real hardware on purpose, even on a Pi); this is "for real, but this
particular machine genuinely has no relay to drive."
"""

import logging

from . import config

logger = logging.getLogger(__name__)

_relay = None
_UNAVAILABLE = "unavailable"
_warned_unavailable = False


def _get_relay():
    """Lazy singleton - gpiozero touches the GPIO chip on construction, so
    this shouldn't run at import time (breaks importing this module on a
    non-Pi dev machine even with DRY_RUN unset by accident elsewhere)."""
    global _relay
    if _relay is not None:
        return _relay

    if config.DRY_RUN:
        _relay = "dry-run"
        return _relay

    try:
        from gpiozero import OutputDevice

        _relay = OutputDevice(
            config.ALARM_RELAY_GPIO_PIN,
            active_high=config.ALARM_RELAY_ACTIVE_HIGH,
            initial_value=False,
        )
    except Exception as e:
        # Broad on purpose: gpiozero's own failure mode here varies by
        # platform/version (BadPinFactory when no backend - RPi.GPIO/lgpio/
        # native - is available at all, as on Windows; a permissions error
        # on a Linux box where the user isn't in the right group; etc.) and
        # every one of them means the same thing for us - no real relay to
        # drive on this machine. Anything at this specific hardware-init
        # boundary should degrade, not take the whole listener down with it.
        logger.error(
            "Could not initialize the alarm relay (%s) - this machine has no usable GPIO "
            "for it (e.g. running on a Windows PC instead of the Pi). Alarm state will still "
            "be recorded correctly by the web app; the physical lamp/buzzer just won't fire "
            "from here.",
            e,
        )
        _relay = _UNAVAILABLE
    return _relay


def activate():
    """Energizes the relay - alarm/warning lamp turns on."""
    global _warned_unavailable
    relay = _get_relay()
    if relay == "dry-run":
        logger.info("[DRY RUN] Alarm relay ON (GPIO %s)", config.ALARM_RELAY_GPIO_PIN)
        return
    if relay == _UNAVAILABLE:
        if not _warned_unavailable:
            logger.warning("Alarm relay unavailable on this machine - skipping physical activation.")
            _warned_unavailable = True
        return
    relay.on()
    logger.info("Alarm relay ON (GPIO %s)", config.ALARM_RELAY_GPIO_PIN)


def deactivate():
    """De-energizes the relay - alarm/warning lamp turns off. Called after
    the web app confirms a Master Key Card tap actually cleared an active
    alarm, and also from rfid_listener.py's periodic alarm-status sync loop
    whenever it notices the app no longer considers an alarm active (e.g.
    admin/in_charge cleared it via the software password-override fallback
    instead of a card tap) - see rfid_listener.py for both call sites."""
    global _warned_unavailable
    relay = _get_relay()
    if relay == "dry-run":
        logger.info("[DRY RUN] Alarm relay OFF (GPIO %s)", config.ALARM_RELAY_GPIO_PIN)
        return
    if relay == _UNAVAILABLE:
        if not _warned_unavailable:
            logger.warning("Alarm relay unavailable on this machine - skipping physical deactivation.")
            _warned_unavailable = True
        return
    relay.off()
    logger.info("Alarm relay OFF (GPIO %s)", config.ALARM_RELAY_GPIO_PIN)


def cleanup():
    """Releases the GPIO pin on shutdown so a restart doesn't fight over it."""
    global _relay
    if _relay is not None and _relay not in ("dry-run", _UNAVAILABLE):
        _relay.close()
    _relay = None
