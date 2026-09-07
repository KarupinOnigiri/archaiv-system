"""Picks USB or network for the fixed UHF RFID Reader Antenna automatically,
instead of a manual config flag - whichever transport the antenna is
actually wired to when the listener starts (and reconfigured for, via the
vendor's RFIDDemo tool: USB_HID output mode for a USB run, NET output mode
with a static IP for an Ethernet run) is the one this uses, with no `.env`
edit needed to match.

Deliberately NOT true hot-swapping (unplug USB mid-session, expect it to
silently pick up Ethernet without a restart): each transport module's own
read_tags() already reconnects forever within itself once chosen (see
hid_tag_reader.py / net_tag_reader.py's own docstrings), matching this
project's existing "a reader thread dying is fatal - exit and let a service
manager restart us" philosophy (rfid_listener.py's main() loop). Re-probing
happens on every restart of the whole listener process, which is exactly
when a real deployment would actually be moving the antenna between USB and
Ethernet anyway.
"""

import logging
import time

from . import hid_tag_reader, net_tag_reader

logger = logging.getLogger(__name__)

# How often to re-check for either transport while neither is present yet
# (e.g. the antenna is still powering up, or genuinely isn't connected at
# all). Not the same as either transport's own internal reconnect delay --
# this is the "nothing found on either transport" case, checked before
# either read_tags() loop is ever entered.
_PROBE_INTERVAL_SECONDS = 5


def read_tags(on_tag):
    """Blocks forever. Probes USB first, then network, on every attempt --
    USB first because it's this project's original/default transport and
    because a USB probe is near-instant (a local device enumeration) versus
    a network probe's real connect-timeout cost. Once a transport is found
    present, delegates to that module's own read_tags(), which itself never
    returns under normal operation (it keeps reconnecting on its own).

    If that delegated read_tags() ever does return or raise anyway (a bug,
    or its own resilience logic giving up in a way we didn't anticipate),
    this loop starts over from probing - that path is a safety net, not
    the intended way to switch transports."""
    while True:
        if hid_tag_reader.is_device_present():
            logger.info("USB HID reader detected - using the USB transport.")
            hid_tag_reader.read_tags(on_tag)
            logger.warning("hid_tag_reader.read_tags() returned unexpectedly - re-probing transports.")
            continue

        if net_tag_reader.is_device_present():
            logger.info("Reader reachable over the network - using the network transport.")
            net_tag_reader.read_tags(on_tag)
            logger.warning("net_tag_reader.read_tags() returned unexpectedly - re-probing transports.")
            continue

        logger.info(
            f"No RFID reader found on USB or network yet "
            f"(checking again in {_PROBE_INTERVAL_SECONDS}s) - "
            "is it powered on and configured for the transport it's wired to?"
        )
        time.sleep(_PROBE_INTERVAL_SECONDS)
