"""One-time migration: encrypts an existing plaintext toolroom.db using SQLCipher's export pattern.

Must run before setting DB_ENCRYPTION_KEY in .env -- otherwise the app can't open the still-plaintext file.

Safety: never touches the live file until export + row-count verification succeed on a copy.
The original is renamed, not deleted. --verify-only skips the final replace.

Usage:
    python -c "import secrets; print(secrets.token_hex(32))"             # 1. generate a key
    DB_ENCRYPTION_KEY=<key> python encrypt_existing_db.py --verify-only  # 2. dry run
    DB_ENCRYPTION_KEY=<key> python encrypt_existing_db.py                # 3. actually encrypt (stop the app first)
    # 4. add DB_ENCRYPTION_KEY=<key> to .env, then restart the app
"""

import os
import shutil
import sys

from database import DB_PATH, harden_backup_file

try:
    from sqlcipher3 import dbapi2 as sqlcipher
except ImportError:
    print(
        "ERROR: sqlcipher3 isn't installed. It needs libsqlcipher-dev on "
        "the system first -- see SECURITY.md. Then:\n"
        "  pip install -r requirements-encryption.txt"
    )
    sys.exit(1)

# Independent from the destination check, so a driver bug can't silently agree with itself.
import sqlite3 as plain_sqlite3


PRE_ENCRYPTION_BACKUP_PATH = DB_PATH + ".pre-encryption-backup"
TMP_ENCRYPTED_PATH = DB_PATH + ".encrypting-tmp"


def _get_key():
    key = (os.environ.get("DB_ENCRYPTION_KEY") or "").strip()
    if not key:
        print(
            "ERROR: set DB_ENCRYPTION_KEY in the environment before running "
            "this (see the usage instructions at the top of this file)."
        )
        sys.exit(1)
    if "'" in key:
        print("ERROR: DB_ENCRYPTION_KEY can't contain a single-quote character.")
        sys.exit(1)
    return key


def _table_row_counts(connect_fn, path, key=None):
    """Returns {table: row_count} for every user table at path, via the given driver."""
    conn = connect_fn(path)
    try:
        if key:
            conn.execute(f"PRAGMA key = '{key}'")
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


def encrypt_database(verify_only=False):
    if not os.path.exists(DB_PATH):
        print(f"ERROR: no database found at {DB_PATH}")
        sys.exit(1)

    key = _get_key()

    if os.path.exists(TMP_ENCRYPTED_PATH):
        os.remove(TMP_ENCRYPTED_PATH)

    # Checkpoints WAL first for a consistent snapshot -- don't run this against a live app.
    src = plain_sqlite3.connect(DB_PATH)
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        src.close()

    print(f"Reading table row counts from the plaintext database at {DB_PATH} ...")
    before_counts = _table_row_counts(plain_sqlite3.connect, DB_PATH)
    print(f"  {len(before_counts)} tables, {sum(before_counts.values())} total rows")

    print("Exporting to an encrypted copy (this can take a while on a Pi) ...")
    src = sqlcipher.connect(DB_PATH)  # no key set -- reads the plaintext file as-is
    try:
        src.execute(f"ATTACH DATABASE '{TMP_ENCRYPTED_PATH}' AS encrypted KEY '{key}'")
        src.execute("SELECT sqlcipher_export('encrypted')")
        src.execute("DETACH DATABASE encrypted")
    except Exception as e:
        src.close()
        if os.path.exists(TMP_ENCRYPTED_PATH):
            os.remove(TMP_ENCRYPTED_PATH)
        print(f"ERROR: export failed, nothing was changed: {e}")
        sys.exit(1)
    src.close()

    print("Verifying the encrypted copy matches the original ...")
    after_counts = _table_row_counts(sqlcipher.connect, TMP_ENCRYPTED_PATH, key=key)
    if after_counts != before_counts:
        print("ERROR: row counts don't match after export -- aborting, original untouched.")
        print(f"  before: {before_counts}")
        print(f"  after:  {after_counts}")
        os.remove(TMP_ENCRYPTED_PATH)
        sys.exit(1)
    print(f"  Verified: all {len(after_counts)} tables match ({sum(after_counts.values())} rows).")

    if verify_only:
        os.remove(TMP_ENCRYPTED_PATH)
        print(
            "\n--verify-only: the export worked and verified clean. Nothing on "
            "disk was changed. Re-run without --verify-only to actually encrypt."
        )
        return

    # Rename, not delete -- keeps this reversible if something looks wrong after restart.
    if os.path.exists(PRE_ENCRYPTION_BACKUP_PATH):
        print(
            f"ERROR: {PRE_ENCRYPTION_BACKUP_PATH} already exists (from a "
            "previous run?) -- move or remove it first so it isn't overwritten."
        )
        os.remove(TMP_ENCRYPTED_PATH)
        sys.exit(1)
    shutil.move(DB_PATH, PRE_ENCRYPTION_BACKUP_PATH)
    shutil.move(TMP_ENCRYPTED_PATH, DB_PATH)
    harden_backup_file(PRE_ENCRYPTION_BACKUP_PATH)
    # database.py hardens DB_PATH's permissions itself on the app's next startup.

    # Stale sidecar files next to the freshly encrypted DB would just be confusing.
    for suffix in ("-wal", "-shm"):
        stale = DB_PATH.rsplit(".db", 1)[0] + ".db" + suffix
        if os.path.exists(stale):
            os.remove(stale)

    print(
        f"\nDone. {DB_PATH} is now encrypted.\n"
        f"The original plaintext file is at {PRE_ENCRYPTION_BACKUP_PATH} -- "
        "keep it until you've confirmed the app starts and reads data "
        "correctly with DB_ENCRYPTION_KEY set, then delete it yourself "
        "(it's an unencrypted copy of everything, same sensitivity as the "
        "live DB used to be).\n\n"
        f"Next: add DB_ENCRYPTION_KEY={key} to .env, then restart the app."
    )


if __name__ == "__main__":
    encrypt_database(verify_only="--verify-only" in sys.argv)
