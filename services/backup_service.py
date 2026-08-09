"""Automatic database backups, run from the app's own APScheduler instance
(app.py's `scheduler`) instead of requiring a separate OS-level cron job
or systemd timer to be set up on every deployment. backup_db.py's
standalone CLI entry point is untouched and still works exactly as
before for anyone who prefers scheduling it that way (e.g. backing up to
a mounted USB drive on a schedule the app itself has no reason to know
about) -- this calls that exact same backup_database() function, so
there's only one real backup implementation to keep correct, just two
ways to trigger it.

Runs on a fixed poll (see app.py: `scheduler.add_job(run_scheduled_backup,
"interval", minutes=30)`) rather than being scheduled at exactly
auto_backup_interval_hours itself -- that way, an admin changing the
interval or flipping the enabled toggle on the System page (routes/
staff.py's backup-settings endpoints) takes effect within 30 minutes
instead of only after whatever the *previous*, possibly much longer,
interval had left to run. Whether a given poll actually backs anything
up depends on whether auto_backup_interval_hours has actually elapsed
since the last run -- tracked as an app_settings row
(auto_backup_last_run_at), not an in-memory timestamp, so a restart
doesn't lose track of when the last real backup happened and cause an
immediate redundant one."""

import logging
from datetime import datetime, timedelta

import backup_db
from database import get_db_connection
from services.settings_service import get_auto_backup_enabled, get_auto_backup_interval_hours

logger = logging.getLogger(__name__)

LAST_RUN_SETTING_KEY = "auto_backup_last_run_at"
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_last_backup_at(conn=None):
    """Public (not just internal to this module) -- the System page's
    backup-settings modal (routes/staff.py) reads this to show "Last
    backup: ..." rather than leaving an admin guessing whether the
    scheduler is actually doing anything."""
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
    """Runs an actual backup immediately, unconditionally (no
    enabled/interval check) -- used by both the scheduler's due-run path
    below and the System page's "Backup Now" button (routes/staff.py),
    which should always do it right now regardless of what the schedule
    says. Records the run time either way (a manual backup counts toward
    "when did we last back up" same as a scheduled one -- there's no
    reason for the scheduler to redundantly re-run right after someone
    just did it by hand). Returns True on success, False on failure
    (already logged either way -- callers decide what to tell the user)."""
    try:
        backup_db.backup_database()
    except SystemExit:
        # backup_database() calls sys.exit(1) on failure (missing DB file,
        # an unwritable/unmounted backup directory, or the backup call
        # itself raising -- see backup_db.py) -- caught here so a failed
        # run can't kill the scheduler thread or bubble up as a 500 to
        # whoever clicked "Backup Now"; it's already printed/logged what
        # went wrong.
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
