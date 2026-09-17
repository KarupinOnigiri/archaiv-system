# Deploying to a Raspberry Pi 4B (8GB)

Targets a Pi 4B 8GB specifically (this is what `database.py`'s cache size
and `wsgi.py`'s thread count are tuned for), running Raspberry Pi OS. The
steps below assume Raspberry Pi OS Lite (64-bit), with no desktop
environment - recommended for a headless server. Storage requirements
are comfortably below what a 32GB+ SD card or a USB SSD provides (see the
boot options in step 1); a USB SSD is the better choice where available,
both for speed and reliability.

## 1. OS setup

### Booting from a USB SSD instead of an SD card (recommended where available)

A Pi 4 booting from SSD over USB is noticeably faster and more reliable
than an SD card (no SD corruption-on-power-loss risk, and much better
random I/O for SQLite's WAL mode). Two things are required for this that
SD-card booting does not need:

1. **USB boot support in the Pi's bootloader.** Pi 4 units sold from
   approximately mid-2020 onward have this enabled by default; units
   newer than that can skip to step 2. If uncertain, or if the Pi is
   older, boot once from a spare SD card with a current Raspberry Pi OS
   image, run `sudo rpi-eeprom-update -a` to update the bootloader,
   reboot, then set the SD card aside - it is not needed again after
   this.
2. **Flash Raspberry Pi OS Lite (64-bit) onto the SSD itself**, not an
   SD card - connect the SSD via USB (ideally USB 3.0, the blue port)
   and select it as the target device in Raspberry Pi Imager.

Everything below is identical regardless of whether the device boots
from SD or SSD - the application itself has no awareness of which one it
is running on.

### Flashing

Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager, onto the
SSD (per above) or an SD card. In the imager's settings (gear icon)
before flashing:
- Enable SSH
- Set a hostname (e.g. `toolroom`)
- Set a username/password (the default `pi`/no-password should not be
  left in place)
- Configure WiFi if not using Ethernet

After booting and connecting via SSH:

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y python3 python3-venv python3-pip git
```

Raspberry Pi OS ships Python 3.11 on Bookworm - verify with `python3
--version`; this application requires 3.9+, so any current Raspberry Pi
OS image is sufficient.

## 2. Get the app onto the Pi and set up a dedicated user

Running as its own unprivileged user (not `pi`, not root) ensures that a
bug in the application cannot affect anything outside its own directory.

```bash
sudo useradd -r -m -d /opt/toolroom -s /usr/sbin/nologin toolroom
sudo -u toolroom git clone <repo-url> /opt/toolroom
cd /opt/toolroom
```

(If no repository host is available to clone from, `scp -r` the project
directory over instead, then run `sudo chown -R toolroom:toolroom
/opt/toolroom`.)

## 3. Run the setup script

```bash
sudo -u toolroom chmod +x /opt/toolroom/deploy/setup.sh
sudo -u toolroom /opt/toolroom/deploy/setup.sh
```

This performs everything else needed before the application can run for
the first time: installs system and Python dependencies (**including
SQLCipher**, so the database is encrypted at rest from the start - see
[SECURITY.md](SECURITY.md) for exactly what that does and does not
protect against before relying on it), generates `.env` with a fresh
`SECRET_KEY` and `DB_ENCRYPTION_KEY`, and runs the application once to
create and harden `instance/toolroom.db`.

**The `DB_ENCRYPTION_KEY` printed by the script must be copied somewhere
other than this SD card** (a password manager, a printed note kept
elsewhere - anywhere other than the device it is protecting). There is
no recovery path if the card fails and that key only ever existed on it.

If encryption is not desired (faster setup, no compile step - the
file-permission hardening below still applies either way):

```bash
sudo -u toolroom /opt/toolroom/deploy/setup.sh --no-encryption
```

If a database with real data already exists from an earlier unencrypted
deployment and encryption is to be added now rather than starting fresh,
this script should not be used for that - see
[SECURITY.md](SECURITY.md)'s `tools/encrypt_existing_db.py` walkthrough
instead, which migrates existing data safely (verifies row counts before
making any changes, keeps a backup). Setting `DB_ENCRYPTION_KEY` against
an already-populated plaintext database without migrating it first will
cause the application to fail to start.

Review `/opt/toolroom/.env` afterward - `FLASK_HOST=0.0.0.0` and
`COOKIE_SECURE=0` are already the script's defaults (plain HTTP,
reachable on the LAN); change `COOKIE_SECURE` to `1` only if a TLS
reverse proxy will be placed in front of this later.

(For a manual setup instead of running the script: it is simply
automating `apt-get install` of the packages in
[SECURITY.md](SECURITY.md)'s SQLCipher section, `python3 -m venv .venv`,
`pip install -r requirements.txt -r requirements-encryption.txt`,
copying `.env.example` to `.env` and filling in
`SECRET_KEY`/`DB_ENCRYPTION_KEY`, then `chmod 600 .env`. Each step is
individually safe to run by hand for anyone who prefers to see each
step.)

## 4. First run (sanity check before installing the service)

```bash
cd /opt/toolroom
sudo -u toolroom .venv/bin/python wsgi.py
```

This creates `instance/toolroom.db`, runs all migrations, and starts
serving. From another device on the same network:

```
http://<pi-ip-or-hostname>:5000
```

The login page should load, with a modal showing the one-time
temp-admin credentials (also printed in the terminal). Log in with those
credentials, complete setup, and confirm the dashboard loads. Ctrl+C
stops this once confirmed working - the next step takes over running it
permanently.

## 5. Install as a systemd service

```bash
sudo cp /opt/toolroom/deploy/toolroom.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now toolroom.service
```

Confirm it is running:

```bash
sudo systemctl status toolroom.service
curl http://localhost:5000/health   # {"status": "ok"}
journalctl -u toolroom.service -f   # live logs, Ctrl+C to stop watching
```

It will now start automatically on every boot and restart itself if it
ever crashes (`Restart=on-failure` in the unit file).

If `deploy/toolroom.service`'s paths do not match the actual setup
(different install location, different username than `toolroom`), edit
the copy in `/etc/systemd/system/` directly, then run `sudo systemctl
daemon-reload`.

## 6. HTTPS via Caddy (needed for the RFID Tag Writer)

Only required if admin staff will use the RFID Tag Writer (Add/Edit
Asset -> Scan/Write Tag) - it uses the browser's Web Serial API to talk
directly to each admin's own SR3308 reader/writer, and that API only
works in a "secure context" (HTTPS, or the exact address `localhost`).
Everything else in the application works fine over the plain
`http://<lan-ip>` setup from the earlier steps - this section can be
skipped if that feature is not in use.

```bash
# Install Caddy (see https://caddyserver.com/docs/install for other distros)
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
sudo apt-get install -y caddy
```

Edit `deploy/Caddyfile`, replacing `<server-lan-ip-or-hostname>` - **use
the Pi's mDNS hostname (`hostname`.local, e.g. `toolroom-rpi.local`), not
a raw LAN IP.** A raw IP silently breaks the moment the Pi's address
changes for any reason (DHCP lease renewal, router reboot, or a mobile
hotspot regenerating its whole subnet on restart) - Caddy keeps running,
but remains bound to an address the Pi no longer has, so the application
becomes unreachable at the old address until someone locates the new one
and edits this file by hand. The hostname form has no such failure mode
- mDNS always resolves to whatever IP the Pi currently holds, so this
only needs to be set up once. `avahi-daemon` (the mDNS service this
depends on) ships enabled by default on Raspberry Pi OS - see
`deploy/Caddyfile`'s own comment for the check. Then install it in place
of Caddy's default config and restart:

```bash
sudo cp /opt/toolroom/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy auto-generates and manages its own local certificate for this
(`tls internal` in the Caddyfile) - no certificate files need to be
created manually. The first time each admin visits over HTTPS, their
browser shows a one-time "not secure" warning (the certificate is
self-signed, not issued by a public CA) that must be clicked through;
this is expected and does not recur after the first visit.

Once staff are reaching the application through Caddy
(`https://<that address>`) instead of hitting Waitress on `:5000`
directly, set **both** `COOKIE_SECURE=1` and `TRUSTED_PROXY_HOPS=1` in
`.env` and restart `toolroom.service`. `COOKIE_SECURE=1` alone is not
sufficient here - login cookies need it to match HTTPS, but without
`TRUSTED_PROXY_HOPS=1` as well, Flask sees every single request (every
staff browser, the Pi's own RFID listener) as coming from Caddy's own
loopback connection instead of each real client's actual address, which
silently breaks anything keyed by request address - most importantly the
rate limiter, causing login attempts from different people to share one
counter instead of one each (see `.env.example`'s comments on both
settings for the full explanation).

## 7. Firewall (optional but recommended)

Recommended where the device is not already behind a trusted router
firewall - restricts the Pi to SSH and the application's port (add
`sudo ufw allow 443/tcp` as well if Caddy is set up per the previous
section):

```bash
sudo apt-get install -y ufw
sudo ufw allow 22/tcp
sudo ufw allow 5000/tcp
sudo ufw enable
```

## 8. Verify the Pi-specific tuning actually took effect

```bash
sudo -u toolroom .venv/bin/python -c "
from database import get_db_connection
conn = get_db_connection()
for p in ['journal_mode', 'synchronous', 'cache_size']:
    print(p, conn.execute(f'PRAGMA {p}').fetchone()[0])
"
```

Expected output: `wal`, `1` (NORMAL), `-65536`. See the earlier
performance work (WAL mode, indexes, image resizing, log rotation,
static caching) for what these settings provide - all of it is already
present in the code, with nothing additional to configure.

```bash
ls -la instance/          # drwx------ 
ls -la instance/*.db      # -rw------- 
ls -la .env                # -rw------- 
```

Confirms that `harden_data_permissions()` (`database.py`) ran correctly
- see [SECURITY.md](SECURITY.md) for what this does and does not protect
against, and the optional database-encryption layer on top of it.

## Updating the app later

```bash
cd /opt/toolroom
sudo systemctl stop toolroom.service
sudo -u toolroom git pull
sudo -u toolroom .venv/bin/pip install -r requirements.txt   # if requirements.txt changed
sudo systemctl start toolroom.service
```

Migrations (`migrations/*.sql`) run automatically on the next startup -
no manual step is required.

## Backups

Automatic already, with no setup required. `toolroom.service` running is
sufficient; `services/backup_service.py`'s scheduler backs up to
`backups/` every 30 minutes (checked against the interval configured on
the System page), keeping the last 5. See `backup_db.py`'s docstring for
a second, independent backup path (e.g. to a mounted USB drive) on top
of that - most deployments do not require it.

## Troubleshooting

**Service will not start** - run `journalctl -u toolroom.service -n 50`
for the actual error. Common causes: `SECRET_KEY` missing from `.env`,
incorrect paths/username in `/etc/systemd/system/toolroom.service`, or
the `toolroom` user not owning `/opt/toolroom` (`sudo chown -R
toolroom:toolroom /opt/toolroom`).

**Login succeeds via `curl`/Postman but the browser appears logged-out
after login** - `COOKIE_SECURE=1` while serving plain HTTP. Set it to
`0` in `.env` and restart (see step 3).

**Not reachable from other devices, only from the Pi itself** -
`FLASK_HOST=127.0.0.1` instead of `0.0.0.0` in `.env`, the firewall
(step 6) not allowing port 5000, or another device's own firewall
blocking outbound requests to it.

**`deploy/setup.sh` fails installing/compiling `sqlcipher3`** - this is
the one step in the whole setup that is not a plain `pip install` (no
prebuilt wheel exists for it, see [SECURITY.md](SECURITY.md)), and is
therefore the step most likely to encounter an environment-specific
issue. Re-run with `--no-encryption` to get everything else working
first (encryption can be added later without starting over -
`tools/encrypt_existing_db.py` migrates an existing database in place),
then debug the compile separately: confirm `apt list --installed | grep
sqlcipher` shows `libsqlcipher-dev`, and that `python3 --version` is
3.9+.

**Application starts but `DB_ENCRYPTION_KEY` appears to have no effect,
or errors occur reading the database** - setting the key against a
database that already contained real (unencrypted) data before the key
was set does not encrypt it retroactively; it simply fails to read it
correctly. Only `deploy/setup.sh` (fresh install, empty database) or
`tools/encrypt_existing_db.py` (existing data) produce a correctly
encrypted file - see [SECURITY.md](SECURITY.md).
