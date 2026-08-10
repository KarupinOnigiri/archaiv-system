"""Automatic database backups via the app's own APScheduler instance, so no
separate OS cron/systemd timer is needed per deployment. backup_db.py's
CLI entry point still works standalone too -- both paths call the same
backup_database(), so there's only one real implementation.

Polls every 30 min rather than scheduling exactly at the configured
interval, so admins see interval/enabled changes take effect quickly. The
last-run time is stored as an app_settings row (not in memory) so a
restart doesn't lose track and trigger a redundant backup."""

import logging
from datetime import datetime, timedelta

import backup_db
from database import get_db_connection
from services.settings_service import get_auto_backup_enabled, get_auto_backup_interval_hours

logger = logging.getLogger(__name__)

LAST_RUN_SETTING_KEY = "auto_backup_last_run_at"
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_last_backup_at(conn=None):
    """Public since the System page's backup-settings modal reads this to
    show "Last backup: ..." so admins aren't guessing if it's working."""
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
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (LAST_RUN_SETTING_KEY, when.strftime(_TIMESTAMP_FORMAT)),
    )
    conn.commit()


def run_backup_now():
    """Runs a backup immediately, no enabled/interval check -- used by both
    the scheduler and the "Backup Now" button. Records the run time either
    way, so a manual backup also resets the scheduler's countdown. Returns
    True/False for success; failure is already logged either way."""
    try:
        backup_db.backup_database()
    except SystemExit:
        # backup_database() calls sys.exit(1) on failure; catch it here so it
        # can't kill the scheduler thread or 500 the "Backup Now" request
        logger.warning("Database backup failed.")
        return False

    conn = get_db_connection()
    try:
        _set_last_backup_at(conn, datetime.now())
    finally:
        conn.close()
    return True


def run_scheduled_backup():
    """The actual APScheduler job (app.py). Safe to call as often as the
    scheduler likes -- it's a no-op unless auto-backup is enabled AND
    the configured interval has actually elapsed since the last run."""
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
    run_backup_now()
