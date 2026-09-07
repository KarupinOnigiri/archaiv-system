"""Scheduled maintenance-due sweep - mirrors services/loan_service.py's
sweep_overdue_members() (own connection, runs on a schedule, see app.py) and
services/notification_service.py's sync_loan_notifications() (idempotent
INSERT OR IGNORE against a UNIQUE constraint, so re-running the sweep never
duplicates a notification that's already there).
"""

import logging

from database import get_db_connection
from services.asset_service import get_maintenance_due_assets
from services.settings_service import get_maintenance_reminder_lead_days

logger = logging.getLogger(__name__)


def sweep_maintenance_due():
    """Creates a 'maintenance_due' notification per asset that's due (or
    within the configured lead time) for its next preventive maintenance,
    date-based or usage-interval-based - see get_maintenance_due_assets.
    Broadcast (no recipient_staff_id/recipient_role), same as due_soon/
    overdue - every logged-in staff member sees it in their bell, same as
    those two."""
    conn = get_db_connection()
    try:
        lead_days = get_maintenance_reminder_lead_days(conn)
        due_assets = get_maintenance_due_assets(conn, within_days=lead_days)

        created = 0
        for asset in due_assets:
            if asset["next_maintenance_due"] and asset["next_maintenance_due"] <= _today(conn):
                message = f"{asset['name']} ({asset['asset_id']}) is due for maintenance"
            elif asset["next_maintenance_due"]:
                message = f"{asset['name']} ({asset['asset_id']}) is due for maintenance soon ({asset['next_maintenance_due']})"
            else:
                message = (
                    f"{asset['name']} ({asset['asset_id']}) has reached its maintenance interval "
                    f"({asset['checkouts_since_maintenance']} checkouts since last service)"
                )

            cursor = conn.execute(
                "INSERT OR IGNORE INTO notifications (type, asset_id, message) VALUES ('maintenance_due', ?, ?)",
                (asset["id"], message),
            )
            if cursor.rowcount:
                created += 1

        conn.commit()
        if created:
            logger.info(f"Synced maintenance-due notifications: {created} created")
    except Exception:
        conn.rollback()
        logger.exception("Failed to sweep maintenance-due assets")
    finally:
        conn.close()


def _today(conn):
    return conn.execute("SELECT date('now', 'localtime') AS d").fetchone()["d"]
