"""Reads tag IDs from the fixed UHF RFID Reader Antenna at the monitored
exit.

The reader is configured in USB_HID output mode (see the manufacturer's
RFIDDemo software / user manual, section "5.3.4 Advanced Settings" --
Output Mode). In that mode the reader behaves exactly like a keyboard-wedge
barcode scanner: for each tag it detects, it "types" the tag's ID as
keystrokes followed by Enter into whichever device/window has focus. On
Linux that shows up as its own dedicated input device
(/dev/input/eventN, distinct from any physical keyboard attached to the
Pi), which is why this grabs a specific evdev device by name rather than
reading from a terminal.

This module intentionally does NOT talk to the reader's native RCP/serial
protocol - USB_HID keystroke mode is simpler, has no vendor SDK dependency,
and is exactly the setup already used to verify basic reads worked (see the
setup notes in this project's conversation history / thesis Chapter 3).
"""

import logging
import time

from . import config

logger = logging.getLogger(__name__)

# evdev KEY_* names for digits and uppercase letters - UHF EPC tag IDs are
# hex strings, so this is the only alphabet actually needed. Extend if a
# different Read Type / output format is configured on the reader.
_KEYCODE_CHARS = {
    "KEY_0": "0", "KEY_1": "1", "KEY_2": "2", "KEY_3": "3", "KEY_4": "4",
    "KEY_5": "5", "KEY_6": "6", "KEY_7": "7", "KEY_8": "8", "KEY_9": "9",
    "KEY_A": "A", "KEY_B": "B", "KEY_C": "C", "KEY_D": "D", "KEY_E": "E",
    "KEY_F": "F", "KEY_G": "G", "KEY_H": "H", "KEY_I": "I", "KEY_J": "J",
    "KEY_K": "K", "KEY_L": "L", "KEY_M": "M", "KEY_N": "N", "KEY_O": "O",
    "KEY_P": "P", "KEY_Q": "Q", "KEY_R": "R", "KEY_S": "S", "KEY_T": "T",
    "KEY_U": "U", "KEY_V": "V", "KEY_W": "W", "KEY_X": "X", "KEY_Y": "Y",
    "KEY_Z": "Z", "KEY_MINUS": "-",
}


def _find_device():
    """Finds the reader's input device by matching HID_DEVICE_NAME against
    each candidate's reported name. Raises if none match, rather than
    silently falling back to some other keyboard on the system (which
    would mean the listener starts eating the Pi's own console input)."""
    import evdev

    if not config.HID_DEVICE_NAME:
        raise RuntimeError(
            "HID_DEVICE_NAME is not set - see .env.example. Run "
            "`python -m evdev.evtest` on the Pi (or `cat /proc/bus/input/devices`) "
            "to find the exact device name the reader reports."
        )

    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        if config.HID_DEVICE_NAME.lower() in device.name.lower():
            return device

    raise RuntimeError(
        f"No input device matching HID_DEVICE_NAME='{config.HID_DEVICE_NAME}' was found. "
        "Is the reader plugged in and set to USB_HID output mode?"
    )


def is_device_present():
    """Cheap presence check used by dual_reader.py to decide which transport
    to run - doesn't grab the device (grab() takes exclusive access, which
    read_tags() needs but a mere probe shouldn't claim), just checks whether
    a matching evdev device is currently enumerated."""
    if config.DRY_RUN:
        return True  # dry-run always "finds" the USB path first; see dual_reader.py
    if not config.HID_DEVICE_NAME:
        return False
    try:
        _find_device()
        return True
    except (RuntimeError, ImportError):
        return False


def read_tags(on_tag):
    """Blocks forever, calling on_tag(tag_id_string) once per Enter-terminated
    keystroke sequence the reader sends. Reconnects automatically if the
    device disappears (e.g. a USB replug) instead of dying the whole
    listener process over a transient disconnect."""
    if config.DRY_RUN:
        logger.info("[DRY RUN] hid_tag_reader active - no real device will be opened.")
        while True:
            time.sleep(3600)

    try:
        import evdev
        from evdev import ecodes
    except ImportError:
        # evdev is Linux-only and dual_reader.py's is_device_present() gate
        # should mean this is never actually reached on a platform without
        # it (e.g. Windows) - but returning cleanly here rather than
        # letting an ImportError propagate keeps that true even if this
        # function is ever called directly, bypassing that gate. Matches
        # dual_reader.py's documented contract: a read_tags() that returns
        # unexpectedly just triggers a re-probe, not a crash.
        logger.error("evdev isn't available on this platform - the USB transport can't run here.")
        return

    buffer = []

    while True:
        try:
            device = _find_device()
            logger.info(f"Reading tags from '{device.name}' ({device.path})")
            device.grab()  # exclusive access - keystrokes don't also leak to a console/getty
            try:
                for event in device.read_loop():
                    if event.type != ecodes.EV_KEY or event.value != 1:  # key-down only
                        continue
                    keycode = ecodes.KEY[event.code]
                    if keycode == "KEY_ENTER":
                        if buffer:
                            tag_id = "".join(buffer)
                            buffer.clear()
                            on_tag(tag_id)
                        continue
                    char = _KEYCODE_CHARS.get(keycode)
                    if char:
                        buffer.append(char)
            finally:
                try:
                    device.ungrab()
                except OSError:
                    pass
        except OSError as e:
            logger.warning(f"Lost connection to the RFID reader ({e}); retrying in 3s...")
            buffer.clear()
            time.sleep(3)
        except RuntimeError as e:
            logger.error(f"{e} Retrying in 5s...")
            time.sleep(5)
