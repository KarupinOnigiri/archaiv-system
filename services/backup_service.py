"""Automatic backup via APScheduler; ang backup_db.py's CLI mahimo pa
gihapon standalone. Mag-check kada 30 min, i-store ang oras sa katapusang
backup sa app_settings para dili mawala ang track bisan mag-restart.
"""

import logging
from datetime import datetime, timedelta

import backup_db
from database import get_db_connection
from services.settings_service import get_auto_backup_enabled, get_auto_backup_interval_hours
from utils.logger import log_action

logger = logging.getLogger(__name__)

LAST_RUN_SETTING_KEY = "auto_backup_last_run_at"
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_last_backup_at(conn=None):
    """Public - para makita sa System page ang "Last backup: ..." para sa
    mga admin.

    Returns: datetime or None kung wala pay backup nga na-run."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (LAST_RUN_SETTING_KEY,)
        ).fetchone()
        if row is None:
            return None
        try:
            return datetime.strptime(row["value"], _TIMESTAMP_FORMAT)
        except (ValueError, TypeError):
            return None
    finally:
        if owns_conn:
            conn.close()


def _set_last_backup_at(conn, when):
    """I-record ang `when` isip oras sa katapusang malampuson nga backup.
    Mag-commit dayon ang iyang kaugalingong write."""
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (LAST_RUN_SETTING_KEY, when.strftime(_TIMESTAMP_FORMAT)),
    )
    conn.commit()


def run_backup_now():
    """Mag-backup dayon karon - gigamit sa scheduler UG sa "Backup Now"
    nga buton sa System page.

    Returns: bool - True kung malampuson."""
    try:
        backup_db.backup_database()
    except SystemExit:
        # backup_database() exits(1) on failure - catch it so it doesn't kill this thread.
        logger.warning("Database backup failed.")
        return False

    conn = get_db_connection()
    try:
        _set_last_backup_at(conn, datetime.now())
    finally:
        conn.close()
    return True


def run_scheduled_backup():
    """APScheduler job - walay buhaton kung wala pa naka-enable ang
    auto-backup o wala pa nangagi ang gi-set nga interval."""
    conn = get_db_connection()
    try:
        if not get_auto_backup_enabled(conn):
            return
        interval_hours = get_auto_backup_interval_hours(conn)
        last_run = get_last_backup_at(conn)
        if last_run is not None and (datetime.now() - last_run) < timedelta(hours=interval_hours):
            return
    finally:
        conn.close()

    logger.info("Running automatic database backup...")
    success = run_backup_now()

    # staff_id=None shows as "System" in the Activity Log, distinct from a manual backup's entry.
    log_action(
        staff_id=None,
        action="ran_scheduled_backup" if success else "scheduled_backup_failed",
        target_table="app_settings",
        details="Automatic database backup completed" if success else "Automatic database backup failed - check the server log",
    )
