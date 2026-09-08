"""Auto hard-delete sa archived assets/members/staff nga milapas na sa
archive_purge_days - PERMANENTE, DILI NA MABAWI. Nag-run kada oras; matag
row tangtangon nga separado, mao nga ang mga row nga gi-block sa history
(naay transaction record) magpabilin ra nga archived.
"""

import logging

# Re-exported from database.py, not stdlib - see routes/assets.py's import for why.
from database import get_db_connection, sqlite3
from services.settings_service import get_archive_purge_days
from utils.formatters import full_name
from utils.logger import log_action
from utils.uploads import delete_asset_image, delete_staff_photo

logger = logging.getLogger(__name__)


def _purge_one_asset(conn, asset_id):
    """Tangtangon ang archived nga asset ug ang mga row nga iyaha - False
    kung gi-block sa foreign-key (naay tinuod nga history), gi-rollback
    ang transaction ug magpabilin nga archived ang asset."""
    images = conn.execute(
        "SELECT image_path FROM asset_images WHERE asset_id = ?", (asset_id,)
    ).fetchall()
    conn.execute("DELETE FROM asset_images WHERE asset_id = ?", (asset_id,))
    # Only touches non-open sessions - an active checkout blocks the purge instead.
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
        # Blocked by history/open session - roll back, stays archived.
        conn.rollback()
        return False

    for image in images:
        delete_asset_image(image["image_path"])
    return True


def _purge_one_staff(conn, staff_id, photo_path):
    """Tangtangon ang mga row sa deactivated nga staff account - ang
    tinuod nga history mopugong ani nga account gikan sa ma-purge."""
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
    """Tangtangon ang usa ka deactivated nga member row - ang staff
    tangtangon una sa parehas nga pass, para dili ma-block sa foreign key."""
    try:
        conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return True


def purge_expired_archived_assets(conn=None):
    """PERMANENTE nga tangtangon ang mga asset nga milapas na sa
    archive_purge_days - walay buhaton kung 0 ang setting (Off).

    Returns: int - pila ka asset ang tinuod nga na-purge."""
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

    # staff_id=None marks this system-triggered; one log line per row, not just a count.
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
            details=f"Archived asset {row['asset_id']} is past the {purge_days}-day retention window but has transaction history - left archived instead of purged",
        )
    if purged:
        logger.info(f"Auto-purged {len(purged)} archived asset(s) past the {purge_days}-day retention window.")
    if skipped:
        logger.info(f"Skipped {len(skipped)} archived asset(s) with transaction history past the retention window.")
    return len(purged)


def purge_expired_inactive_staff(conn=None):
    """Pareho ka ideya sa purge_expired_archived_assets, para sa staff -
    nag-run una sa members para dili ma-block sa foreign key.

    Returns: int."""
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
            details=f"Deactivated staff account {row['username']} is past the {purge_days}-day retention window but has activity history - left deactivated instead of purged",
        )
    if purged:
        logger.info(f"Auto-purged {len(purged)} deactivated staff account(s) past the {purge_days}-day retention window.")
    if skipped:
        logger.info(f"Skipped {len(skipped)} deactivated staff account(s) with activity history past the retention window.")
    return len(purged)


def purge_expired_inactive_members(conn=None):
    """Pareho ka ideya sa purge_expired_archived_assets, para sa members -
    nag-run human sa staff.

    Returns: int."""
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
            details=f"Deactivated member {name} is past the {purge_days}-day retention window but has transaction history - left deactivated instead of purged",
        )
    if purged:
        logger.info(f"Auto-purged {len(purged)} deactivated member(s) past the {purge_days}-day retention window.")
    if skipped:
        logger.info(f"Skipped {len(skipped)} deactivated member(s) with transaction history past the retention window.")
    return len(purged)


def run_scheduled_archive_purge():
    """APScheduler job - matag purge_expired_* walay buhaton kung walay
    pa gikinahanglan i-purge."""
    purge_expired_archived_assets()
    purge_expired_inactive_staff()
    purge_expired_inactive_members()
