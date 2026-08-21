# Deploying to a Raspberry Pi 4B (8GB)

Targets a Pi 4B 8GB specifically (that's what `database.py`'s cache size
and `wsgi.py`'s thread count are tuned for) running Raspberry Pi OS. Steps
below assume Raspberry Pi OS Lite (64-bit), no desktop environment --
recommended for a headless server. A 32GB+ SD card is comfortably more
than this app needs; card *speed* (A1/A2-rated) matters more than size.

## 1. OS setup

Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. In the
imager's settings (gear icon) before flashing:
- Enable SSH
- Set a hostname (e.g. `toolroom`)
- Set a username/password (don't leave the default `pi`/no-password)
- Configure WiFi if not using Ethernet

Boot it, SSH in, then:

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y python3 python3-venv python3-pip git
```

Raspberry Pi OS ships Python 3.11 on Bookworm -- check with `python3
--version`; this app needs 3.9+, so any current Raspberry Pi OS image is
fine.

## 2. Get the app onto the Pi and set up a dedicated user

Running as its own unprivileged user (not `pi`, not root) means a bug in
the app can't touch anything outside its own directory.

```bash
sudo useradd -r -m -d /opt/toolroom -s /usr/sbin/nologin toolroom
sudo -u toolroom git clone <your-repo-url> /opt/toolroom
cd /opt/toolroom
```

(No repo host to clone from? `scp -r` the project directory over instead,
then `sudo chown -R toolroom:toolroom /opt/toolroom`.)

## 3. Run the setup script

```bash
sudo -u toolroom chmod +x /opt/toolroom/deploy/setup.sh
sudo -u toolroom /opt/toolroom/deploy/setup.sh
```

This does everything else needed before the app can run for the first
time: installs system + Python dependencies (**including SQLCipher**, so
the database is encrypted at rest from the start -- see
[SECURITY.md](SECURITY.md) for exactly what that does and doesn't
protect against before relying on it), generates `.env` with a fresh
`SECRET_KEY` and `DB_ENCRYPTION_KEY`, and runs the app once to create and
harden `instance/toolroom.db`.

**Copy the `DB_ENCRYPTION_KEY` it prints somewhere other than this SD
card** (a password manager, a printed note in a drawer -- anywhere that
isn't the device it's protecting). There's no recovery path if the card
fails and that key only ever existed on it.

Don't want encryption (faster setup, no compile step -- you're still
covered by the file-permission hardening below either way)?

```bash
sudo -u toolroom /opt/toolroom/deploy/setup.sh --no-encryption
```

Already have a database with real data in it from an earlier
unencrypted deployment and want to add encryption now instead of
starting fresh? Don't use this script for that -- see
[SECURITY.md](SECURITY.md)'s `tools/encrypt_existing_db.py` walkthrough
instead, which migrates existing data safely (verifies row counts before
touching anything, keeps a backup). Setting `DB_ENCRYPTION_KEY` against
an already-populated plaintext database without migrating it first will
fail to start.

Review `/opt/toolroom/.env` afterward -- `FLASK_HOST=0.0.0.0` and
`COOKIE_SECURE=0` are already the script's defaults (plain HTTP,
reachable on the LAN), change `COOKIE_SECURE` to `1` only if you're
putting a TLS reverse proxy in front of this later.

(Prefer doing this by hand instead of running a script? The script is
just automating: `apt-get install` the packages in
[SECURITY.md](SECURITY.md)'s SQLCipher section, `python3 -m venv .venv`,
`pip install -r requirements.txt -r requirements-encryption.txt`, copying
`.env.example` to `.env` and filling in `SECRET_KEY`/`DB_ENCRYPTION_KEY`,
then `chmod 600 .env`. All individually safe to run yourself if you'd
rather see each step.)

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

You should land on the login page, and a modal should pop up with the
one-time temp-admin credentials (also printed in the terminal). Log in
with those, complete setup, confirm the dashboard loads. Ctrl+C to stop
once you've confirmed this works -- the next step takes over running it
permanently.

## 5. Install as a systemd service

```bash
sudo cp /opt/toolroom/deploy/toolroom.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now toolroom.service
```

Check it's actually up:

```bash
sudo systemctl status toolroom.service
curl http://localhost:5000/health   # {"status": "ok"}
journalctl -u toolroom.service -f   # live logs, Ctrl+C to stop watching
```

It'll now start automatically on every boot and restart itself if it
ever crashes (`Restart=on-failure` in the unit file).

If `deploy/toolroom.service`'s paths don't match your setup (different
install location, different username than `toolroom`), edit the copy in
`/etc/systemd/system/` directly, then `sudo systemctl daemon-reload`.

## 6. Firewall (optional but recommended)

Only if you're not already behind a router firewall you trust -- restrict
the Pi to SSH and the app's port:

```bash
sudo apt-get install -y ufw
sudo ufw allow 22/tcp
sudo ufw allow 5000/tcp
sudo ufw enable
```

## 7. Verify the Pi-specific tuning actually took effect

```bash
sudo -u toolroom .venv/bin/python -c "
from database import get_db_connection
conn = get_db_connection()
for p in ['journal_mode', 'synchronous', 'cache_size']:
    print(p, conn.execute(f'PRAGMA {p}').fetchone()[0])
"
```

Expect `wal`, `1` (NORMAL), `-65536`. See the earlier performance work
(WAL mode, indexes, image resizing, log rotation, static caching) for
what these buy you -- all of that is already in the code, nothing extra
to configure for it.

```bash
ls -la instance/          # drwx------ 
ls -la instance/*.db      # -rw------- 
ls -la .env                # -rw------- 
```

Confirms `harden_data_permissions()` (`database.py`) actually ran and did
its job -- see [SECURITY.md](SECURITY.md) for what this does and doesn't
protect against, and the optional database-encryption layer on top of it.

## Updating the app later

```bash
cd /opt/toolroom
sudo systemctl stop toolroom.service
sudo -u toolroom git pull
sudo -u toolroom .venv/bin/pip install -r requirements.txt   # if requirements.txt changed
sudo systemctl start toolroom.service
```

Migrations (`migrations/*.sql`) run automatically on the next startup --
nothing manual needed there.

## Backups

Automatic already -- no setup needed. `toolroom.service` running is
enough; `services/backup_service.py`'s scheduler backs up to
`backups/` every 30 minutes (checked against the interval configured on
the System page), keeping the last 5. See `backup_db.py`'s docstring if
you want a second, independent backup path (e.g. to a mounted USB drive)
on top of that -- most deployments don't need it.

## Troubleshooting

**Service won't start** -- `journalctl -u toolroom.service -n 50` for the
actual error. Common causes: `SECRET_KEY` missing from `.env`, wrong
paths/username in `/etc/systemd/system/toolroom.service`, or the
`toolroom` user not owning `/opt/toolroom` (`sudo chown -R
toolroom:toolroom /opt/toolroom`).

**Can log in via `curl`/Postman but the browser looks logged-out after
login** -- `COOKIE_SECURE=1` while serving plain HTTP. Set it to `0` in
`.env` and restart (see step 3).

**Not reachable from other devices, only from the Pi itself** --
`FLASK_HOST=127.0.0.1` instead of `0.0.0.0` in `.env`, or the firewall
(step 6) not allowing port 5000, or another device's own firewall
blocking outbound requests to it.

**`deploy/setup.sh` fails installing/compiling `sqlcipher3`** -- this is
the one step in the whole setup that isn't a plain `pip install` (no
prebuilt wheel exists for it, see [SECURITY.md](SECURITY.md)), so it's
the most likely thing to hit an environment-specific snag. Re-run with
`--no-encryption` to get everything else working first (encryption can
be added later without starting over -- `tools/encrypt_existing_db.py`
migrates an existing database in place), then debug the compile
separately: check `apt list --installed | grep sqlcipher` actually shows
`libsqlcipher-dev`, and that `python3 --version` is 3.9+.

**App starts but `DB_ENCRYPTION_KEY` doesn't seem to do anything /
errors about not being able to read the database** -- setting the key
against a database that already had real (unencrypted) data in it before
the key was set doesn't encrypt it retroactively, it just fails to read
it correctly. Only `deploy/setup.sh` (fresh install, empty database) or
`tools/encrypt_existing_db.py` (existing data) actually produce a correctly
encrypted file -- see [SECURITY.md](SECURITY.md).
