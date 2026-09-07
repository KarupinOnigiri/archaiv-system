"""Reads tag IDs from the fixed UHF RFID Reader Antenna over its network
(TCP/RCP) interface - the alternative to hid_tag_reader.py's USB_HID/evdev
path, for a deployment where the antenna is too far from the Pi to run USB
and has instead been switched (via the vendor's RFIDDemo tool, over a one-off
USB connection) to NET output mode with a static IP.

Same RCP packet format as every other transport this reader speaks - see
rcp_protocol.py's docstring for where that's documented. The reader is left
in Active work mode, so once connected it pushes a frame on its own the
moment it sees a tag; this module never needs to send it a command.
"""

import logging
import socket
import time

from . import config, rcp_protocol

logger = logging.getLogger(__name__)

# How long a single recv() call blocks before giving the outer loop a chance
# to notice a shutdown/dead-connection - not a read timeout in the sense of
# "no tag seen," since long idle stretches between reads are completely
# normal and shouldn't be treated as an error.
_RECV_TIMEOUT_SECONDS = 5.0


def is_device_present():
    """Cheap reachability probe used by dual_reader.py to decide which
    transport to actually run - opens and immediately closes a TCP
    connection rather than leaving one open, so it doesn't interfere with
    the real read_tags() connection that follows if this transport is
    chosen."""
    if config.DRY_RUN:
        return False  # dry-run always prefers the (also dry-run) USB path first; see dual_reader.py
    if not config.NET_DEVICE_IP:
        return False
    try:
        with socket.create_connection((config.NET_DEVICE_IP, config.NET_DEVICE_PORT), timeout=config.NET_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def read_tags(on_tag):
    """Blocks forever, calling on_tag(tag_id_string) once per tag the reader
    pushes over the network. Reconnects automatically on a dropped
    connection, mirroring hid_tag_reader.read_tags()'s resilience - a
    reboot of the reader or a flaky network link shouldn't kill the whole
    listener process."""
    if config.DRY_RUN:
        logger.info("[DRY RUN] net_tag_reader active - no real connection will be opened.")
        while True:
            time.sleep(3600)

    if not config.NET_DEVICE_IP:
        raise RuntimeError(
            "NET_DEVICE_IP is not set - see .env.example. Required to run the network reader transport."
        )

    buffer = b""
    while True:
        sock = None
        try:
            logger.info(f"Connecting to RFID reader at {config.NET_DEVICE_IP}:{config.NET_DEVICE_PORT}...")
            sock = socket.create_connection(
                (config.NET_DEVICE_IP, config.NET_DEVICE_PORT), timeout=config.NET_PROBE_TIMEOUT_SECONDS
            )
            sock.settimeout(_RECV_TIMEOUT_SECONDS)
            logger.info("Connected - reading tags.")
            buffer = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue  # normal idle period between tag reads, not an error
                if not chunk:
                    raise OSError("Connection closed by the reader")
                buffer += chunk
                frames, buffer = rcp_protocol.parse_frames(buffer)
                for cid1, rtn, info in frames:
                    tag_id = rcp_protocol.extract_tag_id(cid1, rtn, info)
                    if tag_id:
                        on_tag(tag_id)
        except OSError as e:
            logger.warning(f"Lost connection to the RFID reader ({e}); retrying in 3s...")
            time.sleep(3)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
