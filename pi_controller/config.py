"""Configuration for the tool-room headless controller. Everything here comes
from environment variables (loaded from pi_controller/.env, NOT the web app's
own .env - this runs as a separate process, often on the same Pi but
logically independent of the Flask app's config).

See .env.example in this folder for what each of these means and how to set
them for your actual wiring/hardware.
"""

import os
from dotenv import load_dotenv

_here = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_here, ".env"))


def _get_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# Where the Flask app is reachable. On a real deployment this is usually
# http://127.0.0.1:5000 if the app runs on the same Pi, or the Pi's LAN
# address if the app runs elsewhere - but per the thesis's design the app
# and this controller both live on the same tool-room Pi.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")

# Must match the web app's own RFID_LISTENER_KEY (see the main project's
# .env.example) - this is the shared secret that authenticates this
# controller to the two machine-to-machine endpoints, since it has no staff
# login session behind it.
RFID_LISTENER_KEY = os.environ.get("RFID_LISTENER_KEY")

# --- Fixed UHF RFID Reader Antenna (monitored exit) ------------------------

# The reader is configured in USB_HID output mode (see the manufacturer's
# RFIDDemo software / user manual) so it behaves like a keyboard-wedge
# barcode scanner: it "types" the tag ID followed by Enter into whatever has
# input focus. On Linux this shows up as its own /dev/input/eventN device,
# separate from the Pi's normal keyboard (if any) - HID_DEVICE_NAME is
# matched against each candidate device's reported name so the right one is
# grabbed even if the event number changes across reboots. Use
# `python -m evdev.evtest` or `cat /proc/bus/input/devices` on the Pi to find
# the exact name your unit reports, then set it here.
HID_DEVICE_NAME = os.environ.get("HID_DEVICE_NAME", "")

# Repeated reads of the same tag inside this window are treated as one
# detection event, matching the thesis's stated 1-second cooldown/
# de-duplication delay for the continuous reading.
DEDUP_COOLDOWN_SECONDS = _get_float("DEDUP_COOLDOWN_SECONDS", 1.0)

# Alternative to the USB_HID/evdev transport above, for a deployment where
# the antenna sits too far from the Pi to run USB - see dual_reader.py,
# which tries both this and HID_DEVICE_NAME automatically and uses whichever
# is actually present, no manual mode switch needed. Requires the antenna
# itself to be reconfigured (via the vendor's RFIDDemo tool, over a one-off
# USB connection) from USB_HID output mode to NET output mode with a static
# IP first - that's a setting on the reader, this .env value just has to
# match it. 192.168.2.116:49152 are the vendor SDK's own factory defaults
# (see this reader's Java SDK, SioNetClient.java) - change them to match
# whatever you actually set the reader to.
NET_DEVICE_IP = os.environ.get("NET_DEVICE_IP", "")
NET_DEVICE_PORT = _get_int("NET_DEVICE_PORT", 49152)

# How long to wait for a TCP connect (both dual_reader.py's presence probe
# and net_tag_reader.py's real connection) before giving up on this attempt.
NET_PROBE_TIMEOUT_SECONDS = _get_float("NET_PROBE_TIMEOUT_SECONDS", 2.0)

# --- HF/NFC Reader/Writer (Master Key Card alarm deactivation) -------------

# Substring match against the PC/SC reader name reported by pyscard (e.g.
# "ACS ACR122U PICC Interface 0"). Left blank to just use whichever PC/SC
# reader is first/only available.
PCSC_READER_NAME = os.environ.get("PCSC_READER_NAME", "")

# --- Alarm relay (GPIO) -----------------------------------------------------

# BCM pin number wired to the 2-channel relay module that drives the 12V
# warning lamp/buzzer. Adjust to match your actual wiring - nothing in the
# thesis fixes a specific pin, this is a per-build detail.
ALARM_RELAY_GPIO_PIN = _get_int("ALARM_RELAY_GPIO_PIN", 17)

# Some relay modules are active-low (energizing the relay pulls the input
# LOW, not HIGH) - check your specific relay module's datasheet/silkscreen.
ALARM_RELAY_ACTIVE_HIGH = _get_bool("ALARM_RELAY_ACTIVE_HIGH", True)

# When true, GPIO/PC-SC/evdev calls are replaced with console-logged no-ops
# so this can be developed/tested on a regular PC without any of the Pi
# hardware attached. Defaults off - set explicitly for a dev machine.
DRY_RUN = _get_bool("DRY_RUN", False)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
