# pi_controller

The Raspberry Pi headless controller for RFID exit-monitoring and alarm
control, as described in the thesis's Chapter 3 hardware setup. This is a
**separate program from the Flask web app** in this repo - it runs as its
own long-lived process (systemd service, typically), talks to the fixed
UHF RFID Reader Antenna and the tool-room's HF/NFC Reader/Writer directly,
and only communicates with the web app over three HTTP endpoints
(`routes/monitoring.py` in the main app):

- `POST /api/v1/rfid-events` - report one de-duplicated tagged-tool
  detection. The app classifies it (authorized / unauthorized /
  unknown_tag) and tells this controller whether to activate the alarm.
- `POST /api/v1/rfid-events/deactivate` - report a Master Key Card tap.
  The app confirms the card belongs to an admin and clears the alarm state;
  only then does this controller de-energize the relay.
- `GET /api/v1/monitoring/alarm-status` - polled every
  `ALARM_SYNC_POLL_SECONDS` to keep the relay in sync with the app even
  when the alarm was cleared some other way, e.g. an admin/in_charge using
  the app's password-dismiss fallback instead of a real card tap (see
  `rfid_listener.py`'s `_sync_alarm_relay()`).

The web app is the single source of truth for classification and alarm
state. This controller never decides authorized/unauthorized on its own --
it reports raw hardware events and acts on what the app tells it.

## Two transports for the fixed antenna - and why

The fixed UHF reader (S4A UHF-202406 family) supports two ways of getting
tag reads to this Pi, and `dual_reader.py` auto-detects which one is
actually wired up at startup - no manual mode flag to flip in `.env`:

- **USB** (`hid_tag_reader.py`): the reader configured in **USB_HID output
  mode**, behaving exactly like a keyboard-wedge barcode scanner - it types
  each tag's ID followed by Enter into whatever has input focus. On Linux
  that's readable as a plain evdev input device, no vendor driver or binary
  protocol parsing required. The default/original transport, and the
  simplest when the antenna can physically sit near the Pi.
- **Network** (`net_tag_reader.py`): for when the antenna has to sit too far
  from the Pi to run USB. The reader switched to **NET output mode** with a
  static IP instead, reachable over Ethernet. This one *does* speak the
  vendor's real RCP binary protocol (see `rcp_protocol.py`) - confirmed
  against the reader's own protocol spec ("MM Version Reader Control
  Protocol_v1.2.pdf", bundled with its SDK) and cross-checked against the
  SDK's Java reference client for the real connection defaults. It's the
  same protocol regardless of transport (USB/RS485/network all carry
  identical RCP frames per that spec); this project just never needed to
  parse it directly until the network path required it.

Either way, the Master Key Card reader (ACR122U) still uses PC/SC
(`pyscard`), since that's a real smart-card reader with a standard,
documented APDU interface (see ACS's own "Application Programming
Interface" PDF for the ACR122U - `FF CA 00 00 00h` is the "Get Data"
command this module sends to read a card's UID).

The Master Key Card reader (ACR122U) uses PC/SC (`pyscard`) instead, since
that's a real smart-card reader with a standard, documented APDU interface
(see ACS's own "Application Programming Interface" PDF for the ACR122U --
`FF CA 00 00 00h` is the "Get Data" command this module sends to read a
card's UID).

## Setup on the Pi

For the full numbered checklist (OS flashing through systemd install and
real-hardware validation), see **[DEPLOYMENT.md](DEPLOYMENT.md)**. Short
version:

1. Set the fixed UHF reader's output mode using the manufacturer's
   RFIDDemo configuration tool (one-time setup, over USB from any Windows
   PC - see the reader's own user manual): **USB_HID** if the antenna will
   sit near the Pi and run over USB, or **NET** with a static IP if it needs
   to be far enough away to require Ethernet instead. `dual_reader.py`
   detects which one is actually connected at startup, so only that
   section of `.env` needs filling in. Also set an appropriate **Same ID
   interval** there as a first line of de-duplication; this controller
   applies its own `DEDUP_COOLDOWN_SECONDS` on top regardless.
2. Install system packages the Python libraries need:
   ```bash
   sudo apt install pcscd libpcsclite-dev python3-dev
   sudo usermod -aG gpio,input,plugdev pi
   ```
3. Create a **separate** virtual environment for this controller (don't
   mix its dependencies into the web app's own `.venv` - gpiozero/evdev/
   pyscard have nothing to do with the Flask app and shouldn't end up on
   a machine that's just running the web UI, e.g. a staff laptop):
   ```bash
   python3 -m venv .venv-pi-controller
   .venv-pi-controller/bin/pip install -r pi_controller/requirements.txt
   ```
4. Copy `pi_controller/.env.example` to `pi_controller/.env` and fill it
   in - in particular, `RFID_LISTENER_KEY` must be the **exact same
   value** as `RFID_LISTENER_KEY` in the main app's own `.env`, and either
   `HID_DEVICE_NAME` (find it with `cat /proc/bus/input/devices` after
   plugging the reader in) or `NET_DEVICE_IP`/`NET_DEVICE_PORT` needs to
   match whichever transport mode you set the reader to in step 1.
5. Test it directly first (`Ctrl+C` to stop):
   ```bash
   python -m pi_controller.rfid_listener
   ```
6. Once it's reading tags and reporting to the app correctly, install it as
   a service: copy `pi_controller/toolroom-rfid-listener.service` to
   `/etc/systemd/system/`, edit the paths/user to match your install, then
   `sudo systemctl enable --now toolroom-rfid-listener`.

## Running on a Windows PC/laptop instead of the Pi

Everything here works on Windows **except the alarm relay** - that's wired
directly into the Pi's GPIO pins, and a regular PC simply has no GPIO header
to plug it into. There's no software workaround for that one; it needs
actual GPIO hardware (the Pi, or another SBC with one).

Everything else genuinely runs on Windows:

- **The antenna over the network** (`net_tag_reader.py`) - plain TCP, no
  OS-specific dependency at all.
- **The Master Key Card reader** (`master_key_reader.py`) - PC/SC is a
  cross-platform standard, native on Windows via its own Smart Card
  service, not just a Linux thing.
- The antenna's **USB transport** (`hid_tag_reader.py`) is the one exception
  on the *reading* side - it's built on `evdev`, which is Linux-only. If
  the antenna's on Windows, it needs to be in the reader's NET output mode,
  not USB_HID (see `.env.example`'s `NET_DEVICE_IP`/`NET_DEVICE_PORT`).

`pip install -r pi_controller/requirements.txt` works as-is on Windows --
`evdev` is marked Linux-only in that file so pip just skips it there instead
of failing the whole install. If GPIO hardware isn't available when the
alarm relay is actually needed (`gpio_relay.py`), the controller logs a
clear warning and keeps running rather than crashing - the web app's own
`rfid_events`/audit trail still records alarm activations correctly either
way, only the physical lamp/buzzer doesn't fire from a machine with nothing
to fire it on. Same idea for `master_key_reader.py` if `pyscard` isn't
installed/no card reader is attached: it logs once and idles instead of
taking the whole listener down.

## Developing without the Pi hardware

Set `DRY_RUN=1` in `pi_controller/.env`. Every GPIO/evdev/PC-SC call becomes
a console-logged no-op, so `python -m pi_controller.rfid_listener` runs
(and stays running) on a regular PC. It still calls the real
`api_client` functions if `RFID_LISTENER_KEY`/`APP_BASE_URL` point at a
running instance of the web app, so you can exercise the full
detect -> classify -> alarm -> deactivate loop by POSTing test events
directly (see the app's own `routes/monitoring.py` for the request shapes),
without touching any hardware.

## Files

| File | Purpose |
|---|---|
| `rfid_listener.py` | Entry point - runs both reader loops, wires their events to the API + relay. |
| `dual_reader.py` | Auto-detects USB vs. network for the fixed UHF antenna, delegates to whichever's present. |
| `hid_tag_reader.py` | Reads tag IDs from the fixed UHF antenna over USB (evdev, USB_HID mode). |
| `net_tag_reader.py` | Reads tag IDs from the fixed UHF antenna over the network (TCP, RCP protocol). |
| `rcp_protocol.py` | The vendor's binary RCP frame format - parsing/checksum, shared by `net_tag_reader.py`. |
| `master_key_reader.py` | Watches for a Master Key Card tap (pyscard, PC/SC). |
| `gpio_relay.py` | Drives the alarm relay (gpiozero). |
| `api_client.py` | POSTs to the web app's two listener endpoints. |
| `config.py` | Reads `pi_controller/.env`. |
| `toolroom-rfid-listener.service` | Example systemd unit. |
| `DEPLOYMENT.md` | Full numbered setup checklist for real Pi hardware. |
