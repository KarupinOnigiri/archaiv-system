# pi_controller

The Raspberry Pi headless controller for RFID exit-monitoring and alarm
control, as described in the thesis's Chapter 3 hardware setup. This is a
**separate program from the Flask web app** in this repository - it runs
as its own long-lived process (typically a systemd service), communicates
directly with the fixed UHF RFID Reader Antenna and the tool room's
HF/NFC Reader/Writer, and only communicates with the web app over three
HTTP endpoints (`routes/monitoring.py` in the main app):

- `POST /api/v1/rfid-events` - reports one de-duplicated tagged-tool
  detection. The application classifies it (authorized / unauthorized /
  unknown_tag) and tells this controller whether to activate the alarm.
- `POST /api/v1/rfid-events/deactivate` - reports a Master Key Card tap.
  The application confirms the card belongs to an admin and clears the
  alarm state; only then does this controller de-energize the relay.
- `GET /api/v1/monitoring/alarm-status` - polled every
  `ALARM_SYNC_POLL_SECONDS` to keep the relay in sync with the
  application even when the alarm was cleared some other way, e.g. an
  admin/in_charge using the application's password-dismiss fallback
  instead of a real card tap (see `rfid_listener.py`'s
  `_sync_alarm_relay()`).

The web application is the single source of truth for classification and
alarm state. This controller never decides authorized/unauthorized on
its own - it reports raw hardware events and acts on what the
application tells it.

## Two transports for the fixed antenna, and why

The fixed UHF reader (S4A UHF-202406 family) supports two ways of
delivering tag reads to this Pi, and `dual_reader.py` auto-detects which
one is actually wired up at startup - no manual mode flag needs to be
set in `.env`:

- **USB** (`hid_tag_reader.py`): the reader configured in **USB_HID
  output mode**, behaving exactly like a keyboard-wedge barcode
  scanner - it types each tag's ID followed by Enter into whatever has
  input focus. On Linux this is readable as a plain evdev input device,
  requiring no vendor driver or binary protocol parsing. This is the
  default/original transport, and the simplest option when the antenna
  can be physically located near the Pi.
- **Network** (`net_tag_reader.py`): for cases where the antenna must be
  located too far from the Pi to run USB. The reader is switched to
  **NET output mode** with a static IP instead, reachable over Ethernet.
  This transport speaks the vendor's real RCP binary protocol (see
  `rcp_protocol.py`), confirmed against the reader's own protocol
  specification ("MM Version Reader Control Protocol_v1.2.pdf", bundled
  with its SDK) and cross-checked against the SDK's Java reference
  client for the real connection defaults. The protocol is identical
  regardless of transport (USB/RS485/network all carry identical RCP
  frames per that specification); this project simply had no need to
  parse it directly until the network path required it.

Either way, the Master Key Card reader (ACR122U) uses PC/SC (`pyscard`),
since it is a standard smart-card reader with a documented APDU
interface (see ACS's own "Application Programming Interface" PDF for the
ACR122U - `FF CA 00 00 00h` is the "Get Data" command this module sends
to read a card's UID).

## Setup on the Pi

For the full numbered checklist (OS flashing through systemd install and
real-hardware validation), see **[DEPLOYMENT.md](DEPLOYMENT.md)**.
Summary:

1. Set the fixed UHF reader's output mode using the manufacturer's
   RFIDDemo configuration tool (one-time setup, over USB from any
   Windows PC - see the reader's own user manual): **USB_HID** if the
   antenna will sit near the Pi and run over USB, or **NET** with a
   static IP if it needs to be far enough away to require Ethernet
   instead. `dual_reader.py` detects which one is actually connected at
   startup, so only that section of `.env` needs to be filled in. An
   appropriate **Same ID interval** should also be set there as a first
   line of de-duplication; this controller applies its own
   `DEDUP_COOLDOWN_SECONDS` on top regardless.
2. Install the system packages the Python libraries require:
   ```bash
   sudo apt install pcscd libpcsclite-dev python3-dev
   sudo usermod -aG gpio,input,plugdev pi
   ```
3. Create a **separate** virtual environment for this controller (its
   dependencies should not be mixed into the web app's own `.venv` -
   gpiozero/evdev/pyscard have nothing to do with the Flask app and
   should not end up on a machine that only runs the web UI, e.g. a
   staff laptop):
   ```bash
   python3 -m venv .venv-pi-controller
   .venv-pi-controller/bin/pip install -r pi_controller/requirements.txt
   ```
4. Copy `pi_controller/.env.example` to `pi_controller/.env` and fill it
   in. In particular, `RFID_LISTENER_KEY` must be the **exact same
   value** as `RFID_LISTENER_KEY` in the main application's own `.env`,
   and either `HID_DEVICE_NAME` (found with `cat
   /proc/bus/input/devices` after plugging the reader in) or
   `NET_DEVICE_IP`/`NET_DEVICE_PORT` must match whichever transport mode
   the reader was set to in step 1.
5. Test it directly first (`Ctrl+C` to stop):
   ```bash
   python -m pi_controller.rfid_listener
   ```
6. Once it is reading tags and reporting to the application correctly,
   install it as a service: copy
   `pi_controller/toolroom-rfid-listener.service` to
   `/etc/systemd/system/`, edit the paths/user to match the actual
   install, then run `sudo systemctl enable --now
   toolroom-rfid-listener`.

## Running on a Windows PC/laptop instead of the Pi

Everything here works on Windows **except the alarm relay** - it is
wired directly into the Pi's GPIO pins, and a regular PC has no GPIO
header to connect it to. There is no software workaround for this; it
requires actual GPIO hardware (the Pi, or another SBC with one).

Everything else runs on Windows without modification:

- **The antenna over the network** (`net_tag_reader.py`) - plain TCP,
  with no OS-specific dependency.
- **The Master Key Card reader** (`master_key_reader.py`) - PC/SC is a
  cross-platform standard, native on Windows via its own Smart Card
  service, not a Linux-only mechanism.
- The antenna's **USB transport** (`hid_tag_reader.py`) is the one
  exception on the *reading* side - it is built on `evdev`, which is
  Linux-only. If the antenna is connected to a Windows machine, it must
  be set to the reader's NET output mode rather than USB_HID (see
  `.env.example`'s `NET_DEVICE_IP`/`NET_DEVICE_PORT`).

`pip install -r pi_controller/requirements.txt` works as-is on Windows -
`evdev` is marked Linux-only in that file, so pip simply skips it there
rather than failing the whole install. If GPIO hardware is unavailable
when the alarm relay would otherwise be needed (`gpio_relay.py`), the
controller logs a clear warning and continues running rather than
crashing - the web application's own `rfid_events`/audit trail still
records alarm activations correctly either way; only the physical
lamp/buzzer does not fire on a machine with nothing to fire it through.
The same applies to `master_key_reader.py` when `pyscard` is not
installed or no card reader is attached: it logs once and idles rather
than bringing down the whole listener.

## Developing without the Pi hardware

Set `DRY_RUN=1` in `pi_controller/.env`. Every GPIO/evdev/PC-SC call
becomes a console-logged no-op, allowing `python -m
pi_controller.rfid_listener` to run (and continue running) on a regular
PC. It still calls the real `api_client` functions if
`RFID_LISTENER_KEY`/`APP_BASE_URL` point at a running instance of the
web application, so the full detect -> classify -> alarm -> deactivate
loop can be exercised by posting test events directly (see the
application's own `routes/monitoring.py` for the request shapes),
without touching any hardware.

## Files

| File | Purpose |
|---|---|
| `rfid_listener.py` | Entry point - runs both reader loops, wires their events to the API and relay. |
| `dual_reader.py` | Auto-detects USB vs. network for the fixed UHF antenna, delegating to whichever is present. |
| `hid_tag_reader.py` | Reads tag IDs from the fixed UHF antenna over USB (evdev, USB_HID mode). |
| `net_tag_reader.py` | Reads tag IDs from the fixed UHF antenna over the network (TCP, RCP protocol). |
| `rcp_protocol.py` | The vendor's binary RCP frame format - parsing and checksum, shared by `net_tag_reader.py`. |
| `master_key_reader.py` | Watches for a Master Key Card tap (pyscard, PC/SC). |
| `gpio_relay.py` | Drives the alarm relay (gpiozero). |
| `api_client.py` | Posts to the web application's two listener endpoints. |
| `config.py` | Reads `pi_controller/.env`. |
| `toolroom-rfid-listener.service` | Example systemd unit. |
| `DEPLOYMENT.md` | Full numbered setup checklist for real Pi hardware. |
