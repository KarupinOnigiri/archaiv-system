# Database security on deployment

This application's data (member names, phone numbers, RFID card IDs,
restriction reasons, staff usernames, password/PIN hashes, etc.) resides
entirely in one SQLite file: `instance/toolroom.db`. Two independent
layers protect it, and it is important to understand which threat each
one actually covers.

## What is already in place

- **The web application itself** is locked down regardless of the layers
  described below: passwords and RFID PINs are hashed (never stored in
  plaintext), sessions are server-side with CSRF protection, and every
  action is role-gated.
- **`.gitignore`** already excludes `.env`, `instance/`, `*.db`, and
  `backups/` from source control.

Neither of those protects the raw database *file* from someone who
obtains direct access to the device it is running on. That is what the
rest of this document covers.

## Layer 1: OS file permissions (automatic, always on)

`database.py`'s `harden_data_permissions()` runs on every application
startup and restricts `instance/` (700) and the database file plus its
WAL/SHM sidecars (600) to the OS user the application runs as.
`backup_db.py` applies the same restriction to every backup file it
writes. `.env` (holding `SECRET_KEY` and, if enabled,
`DB_ENCRYPTION_KEY`) receives the same 600 treatment.

**This is a no-op on Windows** (chmod bits have no effect on NTFS) - it
takes effect only on the Pi/Linux deployment target.

**What it protects against:** another OS user account on the same Pi
reading the database; the `instance/` or `backups/` folder ever becoming
reachable over a file share (SMB/SFTP) or a mounted USB drive with loose
permissions.

**What it does NOT protect against:** someone who obtains the entire SD
card (the Pi is stolen, the card is removed, the block device is
imaged). Permissions are enforced by the OS reading the filesystem;
reading the same bytes on a different machine renders them meaningless.

**Nothing to configure.** Verify the permissions after the first deploy:

```bash
ls -la instance/          # should show drwx------ on the directory
ls -la instance/*.db      # should show -rw------- on the files
ls -la .env                # should show -rw-------
```

If ownership is incorrect (for example, the application was deployed as
root once, then switched to a dedicated service user), correct it
manually:

```bash
sudo chown -R youruser:youruser /path/to/app/instance /path/to/app/.env
```

## Layer 2: Encryption at rest (opt-in, `DB_ENCRYPTION_KEY`)

Encrypts the database file itself (AES-256 via SQLCipher), so that a
*copy* of `toolroom.db` - obtained over a compromised network share,
lifted from an unsecured backup, or otherwise - is unreadable without
the key.

### The limitation that must be understood before enabling this

This application auto-starts unattended, which is the point of a Pi
kiosk/server deployment: it must come back up on its own after a power
loss, with no one present to enter credentials. An unattended process
requires its decryption key to be available somewhere it can read
without human input, and the only such location on this device is the
same SD card the encrypted database resides on (`.env`, alongside it).

Consequently: **if the entire SD card is stolen, the key is obtained
along with the encrypted data, and encryption does not stop an
attacker.** It only mitigates *partial* access - someone who obtains the
database file but not the accompanying `.env`.

The actual defense against whole-device theft is physical: a locked
enclosure, with the Pi bolted down or secured in a cabinet rather than
left exposed. No software change addresses that; it is worth doing
regardless of whether encryption is enabled.

### Setup (Raspberry Pi OS / Debian)

**On a fresh Pi deployment with no existing data, `deploy/setup.sh`
already performs all of the steps below by default** (run with
`--no-encryption` to skip it) - see DEPLOYMENT.md step 3. The manual
steps that follow are what it automates, and are useful for
understanding the process, troubleshooting a failure, or adding
encryption to a Pi that is already running unencrypted. In the latter
case, skip ahead to "Existing database with real data" below rather than
starting over, since `deploy/setup.sh` is intended only for a
fresh/empty database.

SQLCipher has no prebuilt Python wheel for any platform, so this
compiles from source, which requires system packages first:

```bash
sudo apt-get update
sudo apt-get install -y libsqlcipher-dev libssl-dev build-essential python3-dev
pip install -r requirements-encryption.txt
```

Generate a key (32 random bytes, hex-encoded; a passphrase entered by
hand should be avoided, as this key is never typed manually, and a hex
string also avoids the single-quote restriction described below):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**Existing database with real data:** encrypt it first. Do not simply
set `DB_ENCRYPTION_KEY` and restart, as the application will attempt to
read the still-plaintext file as if it were encrypted and fail:

```bash
# Stop the application first - this is not intended to run against a live DB.
DB_ENCRYPTION_KEY=<key> python tools/encrypt_existing_db.py --verify-only  # dry run, changes nothing
DB_ENCRYPTION_KEY=<key> python tools/encrypt_existing_db.py                 # performs the encryption
```

That script backs up the original to
`instance/toolroom.db.pre-encryption-backup` (renamed, not deleted)
before making any changes, and verifies that every table's row count
matches before replacing the live file. That backup should be retained
until the application is confirmed to be working correctly, then removed
manually.

**Starting fresh** (no existing data): set the key and start the
application. `init_db()` creates a new database, and with
`DB_ENCRYPTION_KEY` set from the first connection, it is encrypted from
the start.

In either case, once the database side is complete:

```bash
# .env
DB_ENCRYPTION_KEY=<key>
```

Restart the application and confirm that it starts cleanly and that
login succeeds.

### Backups

`backup_db.py` reads `DB_ENCRYPTION_KEY` the same way the application
does, and produces encrypted backups when it is set, with no separate
configuration required. A backup is exactly as sensitive as the live
database, since it is a full copy - which is also why
`harden_data_permissions()`/`harden_backup_file()` restrict those files
as well, independent of whether encryption is enabled.

### Restoring an encrypted backup

Same procedure as any backup: copy the file to `instance/toolroom.db`
with the application stopped. Files produced by
`sqlite3.Connection.backup()` are self-contained, so there are no
separate `-wal`/`-shm` files to account for. `DB_ENCRYPTION_KEY` must
already be set to the same key for the application to open it
afterward.
