"""Auto hard-deletes archived/deactivated records via APScheduler (app.py),
same shape as backup_service.py's automatic backups. Covers assets
(deleted_at), members and staff (is_active=0/deactivated_at) -- these used
to sit soft-deleted forever with nothing to ever purge them.

One admin setting (archive_purge_days) governs all three; past that many
days, rows get a plain DELETE -- unlike the soft-delete/deactivate itself,
this is NOT reversible. Runs hourly rather than being scheduled exactly at
the deadline, so changing the retention window takes effect within an hour;
there's no "last run" timestamp to track, a missed poll just catches up
next time.

Rows with real history (transactions, audit_log, etc, all FK-enforced) raise
sqlite3.IntegrityError on DELETE, so each candidate is purged independently
-- one blocked row just stays archived instead of blocking the rest of the
batch or corrupting history. In practice this means anything with real
usage history basically never auto-purges, which is the point: this clears
stale unused records, not an app-wide "forget everything" switch."""

import logging
import sqlite3

from database import get_db_connection
from services.settings_service import get_archive_purge_days
from utils.formatters import full_name
from utils.logger import log_action
from utils.uploads import delete_asset_image, delete_staff_photo

logger = logging.getLogger(__name__)


def _purge_one_asset(conn, asset_id):
    """Deletes everything safe to delete for one archived asset, then the
    asset row itself. Returns True if removed, False if blocked by
    transaction history. Runs inside the caller's transaction."""
    images = conn.execute(
        "SELECT image_path FROM asset_images WHERE asset_id = ?", (asset_id,)
    ).fetchall()
    conn.execute("DELETE FROM asset_images WHERE asset_id = ?", (asset_id,))
    # only touch checkout_session_items on sessions that are no longer open --
    # an active checkout should block the purge, not get deleted out from under it
    conn.execute(
        """
        DELETE FROM checkout_session_items
        WHERE asset_id = ? AND checkout_session_id IN (
            SELECT id FROM checkout_sessions WHERE status != 'open'
        )
        """,
        (asset_id,),
    )

    try:
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    except sqlite3.IntegrityError:
        # blocked by history/open session -- roll back so it's untouched, still archived
        conn.rollback()
        return False

    for image in images:
        delete_asset_image(image["image_path"])
    return True


def _purge_one_staff(conn, staff_id, photo_path):
    """Deletes a deactivated staff account's own non-historical rows, then
    the staff row itself. Sessions are cleared first (a stray un-expired
    one would FK-block the delete); everything else referencing staff is
    real history and should keep the account un-purgeable."""
    conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff_id,))

    try:
        conn.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
    except sqlite3.IntegrityError:
        conn.rollback()
        return False

    if photo_path:
        delete_staff_photo(photo_path)
    return True


def _purge_one_member(conn, member_id):
    """Deletes a single deactivated member's row -- no owned files/side
    rows to clean up first, unlike assets/staff. Staff is purged before
    members in the same pass, so any linked staff row is already gone or
    still has history blocking it via the staff.member_id FK."""
    try:
        conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return True


def purge_expired_archived_assets(conn=None):
    """Permanently deletes every eligible asset whose deleted_at is older
    than the configured archive_purge_days. No-op (returns 0) if the
    setting is 0 ("None" -- auto-purge disabled). Returns the number of
    assets actually purged (assets skipped for having transaction
    history don't count, and stay archived)."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        purge_days = get_archive_purge_days(conn)
        if purge_days <= 0:
            return 0

        candidates = conn.execute(
            """
            SELECT id, asset_id, name FROM assets
            WHERE deleted_at IS NOT NULL
            AND deleted_at <= datetime('now', 'localtime', ?)
            """,
            (f"-{purge_days} days",),
        ).fetchall()

        purged, skipped = [], []
        for row in candidates:
            if _purge_one_asset(conn, row["id"]):
                conn.commit()
                purged.append(row)
            else:
                skipped.append(row)
    finally:
        if owns_conn:
            conn.close()

    # staff_id=None marks this as system-triggered, same as other scheduled sweeps;
    # one log line per row so each purge/skip shows up individually, not just a count
    for row in purged:
        log_action(
            staff_id=None,
            action="auto_purged_archived_asset",
            target_table="assets",
            target_id=row["id"],
            target_name=row["name"],
            details=f"Permanently deleted archived asset {row['asset_id']} after {purge_days} day(s) in the archive",
        )
    for row in skipped:
        log_action(
            staff_id=None,
            action="auto_purge_skipped_archived_asset",
            target_table="assets",
            target_id=row["id"],
            target_name=row["name"],
            details=f"Archived asset {row['asset_id']} is past the {purge_days}-day retention window but has transaction history -- left archived instead of purged",
        )
    if purged:
        logger.info(f"Auto-purged {len(purged)} archived asset(s) past the {purge_days}-day retention window.")
    if skipped:
        logger.info(f"Skipped {len(skipped)} archived asset(s) with transaction history past the retention window.")
    return len(purged)


def purge_expired_inactive_staff(conn=None):
    """Same idea as purge_expired_archived_assets, for deactivated staff
    accounts past archive_purge_days. Runs before purge_expired_inactive_members
    so a member's linked staff row is cleared first, avoiding an FK block."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        purge_days = get_archive_purge_days(conn)
        if purge_days <= 0:
            return 0

        candidates = conn.execute(
            """
            SELECT id, username, photo_path FROM staff
            WHERE is_active = 0 AND deactivated_at IS NOT NULL
            AND deactivated_at <= datetime('now', 'localtime', ?)
            """,
            (f"-{purge_days} days",),
        ).fetchall()

        purged, skipped = [], []
        for row in candidates:
            if _purge_one_staff(conn, row["id"], row["photo_path"]):
                conn.commit()
                purged.append(row)
            else:
                skipped.append(row)
    finally:
        if owns_conn:
            conn.close()

    for row in purged:
        log_action(
            staff_id=None,
            action="auto_purged_inactive_staff",
            target_table="staff",
            target_id=row["id"],
            target_name=row["username"],
            details=f"Permanently deleted deactivated staff account {row['username']} after {purge_days} day(s) inactive",
        )
    for row in skipped:
        log_action(
            staff_id=None,
            action="auto_purge_skipped_inactive_staff",
            target_table="staff",
            target_id=row["id"],
            target_name=row["username"],
            details=f"Deactivated staff account {row['username']} is past the {purge_days}-day retention window but has activity history -- left deactivated instead of purged",
        )
    if purged:
        logger.info(f"Auto-purged {len(purged)} deactivated staff account(s) past the {purge_days}-day retention window.")
    if skipped:
        logger.info(f"Skipped {len(skipped)} deactivated staff account(s) with activity history past the retention window.")
    return len(purged)


def purge_expired_inactive_members(conn=None):
    """Same idea as purge_expired_archived_assets, for deactivated members
    past archive_purge_days. Runs after purge_expired_inactive_staff."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        purge_days = get_archive_purge_days(conn)
        if purge_days <= 0:
            return 0

        candidates = conn.execute(
            """
            SELECT id, first_name, last_name, middle_name, suffix FROM members
            WHERE is_active = 0 AND deactivated_at IS NOT NULL
            AND deactivated_at <= datetime('now', 'localtime', ?)
            """,
            (f"-{purge_days} days",),
        ).fetchall()

        purged, skipped = [], []
        for row in candidates:
            if _purge_one_member(conn, row["id"]):
                conn.commit()
                purged.append(row)
            else:
                skipped.append(row)
    finally:
        if owns_conn:
            conn.close()

    for row in purged:
        name = full_name(row["first_name"], row["last_name"], row["middle_name"], row["suffix"])
        log_action(
            staff_id=None,
            action="auto_purged_inactive_member",
            target_table="members",
            target_id=row["id"],
            target_name=name,
            details=f"Permanently deleted deactivated member {name} after {purge_days} day(s) inactive",
        )
    for row in skipped:
        name = full_name(row["first_name"], row["last_name"], row["middle_name"], row["suffix"])
        log_action(
            staff_id=None,
            action="auto_purge_skipped_inactive_member",
            target_table="members",
            target_id=row["id"],
            target_name=name,
            details=f"Deactivated member {name} is past the {purge_days}-day retention window but has transaction history -- left deactivated instead of purged",
        )
    if purged:
        logger.info(f"Auto-purged {len(purged)} deactivated member(s) past the {purge_days}-day retention window.")
    if skipped:
        logger.info(f"Skipped {len(skipped)} deactivated member(s) with transaction history past the retention window.")
    return len(purged)


def run_scheduled_archive_purge():
    """The actual APScheduler job. Safe to call as often as the scheduler
    likes -- each purge_expired_* is a no-op when nothing's due. Staff
    before members (see purge_expired_inactive_staff); assets are unordered."""
    purge_expired_archived_assets()
    purge_expired_inactive_staff()
    purge_expired_inactive_members()
