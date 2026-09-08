"""Automatic backups via the app's own APScheduler; backup_db.py's CLI still works standalone.
Polls every 30 min, and stores the last-run time in app_settings so a restart doesn't lose track.

(Bisaya: Kini nga file ang naghimo sa automatic backup, gigamit ang
APScheduler nga naa na sa app - ang backup_db.py's CLI - command line -
gigamit gihapon ug mahimo pa gihapon standalone, dili apektado ni. Mag-check
kini kada 30 minutos, ug i-store ang oras sa katapusang backup sa
app_settings table para dili mawala ang track bisan mag-restart ang app.)
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
    """Public so the System page can show "Last backup: ..." to admins.
    (Bisaya: Public/pwede gamiton sa gawas ani nga file, para makita sa
    System page ang "Last backup: ..." para sa mga admin.)

    Args:
        conn: an already-open sqlite3.Connection to reuse, or None to open
            (and close) a fresh one just for this call.
        (Bisaya: usa ka naa nay bukas nga koneksyon nga gamiton pag-usab,
        o None para mag-abre - ug mo-close - ug bag-o para ani nga tawag
        ra.)

    Returns:
        datetime or None - when the last backup finished, or None if no
        backup has ever run (or the stored value is corrupted/unparseable).
        (Bisaya: datetime o None - kung kanus-a nahuman ang katapusang
        backup, o None kung wala pa gyuy backup nga na-run - o kung sayop
        ang na-store nga value.)
    """
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
    """Records `when` as the last-successful-backup timestamp in app_settings.
    (Bisaya: I-record ang `when` isip oras sa katapusang malampuson nga
    backup, sulod sa app_settings table.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        when: the datetime the backup finished (usually datetime.now()).
        (Bisaya: ang datetime kung kanus-a nahuman ang backup - kasagaran
        datetime.now().)

    Returns:
        None - commits the write itself.
        (Bisaya: walay gibalik - mag-commit dayon kini sa iyang sinulat.)
    """
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (LAST_RUN_SETTING_KEY, when.strftime(_TIMESTAMP_FORMAT)),
    )
    conn.commit()


def run_backup_now():
    """Runs a backup immediately; used by both the scheduler and the "Backup Now" button.
    (Bisaya: Mag-backup dayon karon - gigamit sa scheduler UG sa "Backup
    Now" nga buton sa System page.)

    Returns:
        bool - True if the backup succeeded (and last-run timestamp was
        updated), False if backup_db.backup_database() failed.
        (Bisaya: True kung malampuson ang backup - na-update pod ang
        timestamp - False kung napakyas ang backup_db.backup_database().)
    """
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
    """The APScheduler job - a no-op unless auto-backup is enabled and its interval has elapsed.
    (Bisaya: Kini ang APScheduler job - walay buhaton kung wala pa
    naka-enable ang auto-backup, o kung wala pa nangagi ang gi-set nga
    interval/panahon. Gitawag ni matag pipila ka minuto sa scheduler; walay
    parameter ug walay return value.)"""
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
