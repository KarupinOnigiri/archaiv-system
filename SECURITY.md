# Database security on deployment

This app's data (member names, phone numbers, RFID card IDs, restriction
reasons, staff usernames, password/PIN hashes, ...) lives entirely in one
SQLite file: `instance/toolroom.db`. Two independent layers protect it,
and it matters which threat each one actually covers.

## What's already in place

- **The web app itself** is locked down regardless of any of this:
  passwords/RFID PINs are hashed (never stored plaintext), sessions are
  server-side with CSRF protection, every action is role-gated.
- **`.gitignore`** already excludes `.env`, `instance/`, `*.db`, and
  `backups/` from source control.

Neither of those protects the raw database *file* from someone who gets
direct access to the device it's running on. That's what the rest of this
document covers.

## Layer 1: OS file permissions (automatic, always on)

`database.py`'s `harden_data_permissions()` runs on every app startup and
restricts `instance/` (700) and the DB file plus its WAL/SHM sidecars
(600) to the OS user the app runs as. `backup_db.py` does the same to
every backup file it writes. `.env` (holding `SECRET_KEY` and, if you
enable it, `DB_ENCRYPTION_KEY`) gets the same 600 treatment.

**This is a no-op on Windows** (chmod bits don't mean anything on
NTFS) -- it only does something on the Pi/Linux deployment target.

**What it protects against:** another OS user account on the same Pi
reading the database; the `instance/` or `backups/` folder ever ending up
reachable over a file share (SMB/SFTP) or a mounted USB drive with loose
permissions.

**What it does NOT protect against:** someone who gets the whole SD card
(steals the Pi, pulls the card, dd's the block device). Permissions are
enforced by the OS reading the filesystem -- read the same bytes on a
different machine and they mean nothing.

**Nothing to configure.** Verify it worked after your first deploy:

```bash
ls -la instance/          # should show drwx------ on the directory
ls -la instance/*.db      # should show -rw------- on the files
ls -la .env                # should show -rw-------
```

If ownership looks wrong (e.g. you deployed as root once, then switched to
a dedicated service user), fix it once manually:

```bash
sudo chown -R youruser:youruser /path/to/app/instance /path/to/app/.env
```

## Layer 2: Encryption at rest (opt-in, `DB_ENCRYPTION_KEY`)

Encrypts the database file itself (AES-256 via SQLCipher), so a *copy* of
`toolroom.db` -- grabbed over a compromised network share, lifted from an
unsecured backup, whatever -- is unreadable without the key.

### The limitation you need to understand before turning this on

This app auto-starts unattended (that's the point of a Pi kiosk/server --
it needs to come back up on its own after a power loss, with nobody there
to type anything in). An unattended process needs its decryption key
available *somewhere it can read without a human*, and the only place
that exists on this device is the same SD card the encrypted database
lives on (`.env`, next to it).

So: **if someone steals the entire SD card, they get the key along with
the encrypted data, and encryption doesn't stop them.** It only helps
against *partial* access -- someone who gets the database file but not
`.env` alongside it.

The actual defense against whole-device theft is physical: a locked
enclosure, the Pi bolted down or in a cabinet, not left sitting out. No
software change fixes that; it's worth doing regardless of whether you
enable encryption.

### Setup (Raspberry Pi OS / Debian)

**On a fresh Pi deployment with no existing data, `deploy/setup.sh`
already does all of this for you** (it's the default -- run with
`--no-encryption` to skip it instead) -- see DEPLOYMENT.md step 3. The
manual steps below are what it's automating, useful if you want to
understand what's happening, troubleshoot a failure, or add encryption
to a Pi that's already running unencrypted (in which case, skip ahead to
"If you have an existing database with real data in it" below instead of
starting over -- `deploy/setup.sh` is only for a fresh/empty database).

SQLCipher has no prebuilt Python wheel for any platform -- this compiles
from source, which needs system packages first:

```bash
sudo apt-get update
sudo apt-get install -y libsqlcipher-dev libssl-dev build-essential python3-dev
pip install -r requirements-encryption.txt
```

Generate a key (32 random bytes, hex-encoded -- avoid a passphrase you
type in by hand, this never gets typed, and a hex string sidesteps the
single-quote restriction below entirely):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**If you have an existing database with real data in it**, encrypt it
first -- do NOT just set `DB_ENCRYPTION_KEY` and restart, the app will try
to read the still-plaintext file as if it were encrypted and fail:

```bash
# Stop the app first -- this isn't meant to run against a live DB.
DB_ENCRYPTION_KEY=<your key> python encrypt_existing_db.py --verify-only  # dry run, changes nothing
DB_ENCRYPTION_KEY=<your key> python encrypt_existing_db.py                 # actually encrypts
```

That script backs up the original to
`instance/toolroom.db.pre-encryption-backup` (renamed, not deleted) before
touching anything, and verifies every table's row count matches before
it'll replace the live file. Keep that backup until you've confirmed the
app works, then delete it yourself.

**If you're starting fresh** (no existing data), just set the key and
start the app -- `init_db()` creates a new database, and with
`DB_ENCRYPTION_KEY` set from the first connection it's encrypted from the
start.

Either way, once the database side is done:

```bash
# .env
DB_ENCRYPTION_KEY=<your key>
```

Restart the app and confirm it starts cleanly and you can log in.

### Backups

`backup_db.py` reads `DB_ENCRYPTION_KEY` the same way the app does, and
produces encrypted backups when it's set -- no separate configuration
needed. A backup is exactly as sensitive as the live database (it's a
full copy), which is also why `harden_data_permissions()`/
`harden_backup_file()` lock those files down too, independent of whether
encryption is on.

### Restoring an encrypted backup

Same as any backup: copy the file to `instance/toolroom.db` with the app
stopped. `sqlite3.Connection.backup()`-produced files are self-contained,
so no separate `-wal`/`-shm` files to worry about. `DB_ENCRYPTION_KEY`
must already be set to the same key for the app to open it afterward.
