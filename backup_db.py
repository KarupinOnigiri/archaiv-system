"""Backs up toolroom.db via SQLite's online backup API, not a plain file copy (WAL splits state across sidecar files).

Usage:
    python backup_db.py                  # backs up to ./backups
    python backup_db.py /mnt/usb/backups  # backs up to a mounted USB drive

Runs automatically every 30 min via services/backup_service.py; see DEPLOYMENT.md for standalone scheduling.
"""

import os
import sys
from datetime import datetime

# Sourced from database.py, not stdlib -- a mismatched driver would silently produce an unencrypted backup.
from database import DB_PATH, DB_ENCRYPTION_KEY, sqlite3, harden_backup_file

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_BACKUP_DIR = os.path.join(ROOT_DIR, "backups")

# Backups to keep before pruning oldest; only ever deletes toolroom_*.db files it wrote itself.
KEEP_LAST_N = 5


def backup_database(backup_dir=DEFAULT_BACKUP_DIR, keep_last_n=KEEP_LAST_N):
    if not os.path.exists(DB_PATH):
        print(f"ERROR: database not found at {DB_PATH}")
        sys.exit(1)

    try:
        os.makedirs(backup_dir, exist_ok=True)
    except OSError as e:
        # Usually means backup_dir is an unmounted USB drive on a Pi -- fail loudly, don't skip silently.
        print(f"ERROR: could not create/access backup directory {backup_dir}: {e}")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"toolroom_{timestamp}.db")

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(backup_path)
    try:
        if DB_ENCRYPTION_KEY:
            # Both src and dst need the key -- one to read encrypted pages, the other to write them.
            src.execute(f"PRAGMA key = '{DB_ENCRYPTION_KEY}'")
            dst.execute(f"PRAGMA key = '{DB_ENCRYPTION_KEY}'")
        with dst:
            src.backup(dst)
        print(f"Backup written to {backup_path}")
    except Exception as e:
        print(f"ERROR: backup failed: {e}")
        # Don't leave a partial/corrupt file around looking like a valid backup.
        if os.path.exists(backup_path):
            os.remove(backup_path)
        sys.exit(1)
    finally:
        src.close()
        dst.close()

    harden_backup_file(backup_path)
    _prune_old_backups(backup_dir, keep_last_n)


def _prune_old_backups(backup_dir, keep_last_n):
    backups = sorted(
        f
        for f in os.listdir(backup_dir)
        if f.startswith("toolroom_") and f.endswith(".db")
    )
    excess = len(backups) - keep_last_n
    for old_file in backups[: max(excess, 0)]:
        os.remove(os.path.join(backup_dir, old_file))
        print(f"Pruned old backup: {old_file}")


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BACKUP_DIR
    backup_database(dest)
