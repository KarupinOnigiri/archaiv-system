"""Scheduled maintenance-due sweep - mirrors services/loan_service.py's
sweep_overdue_members() (own connection, runs on a schedule, see app.py) and
services/notification_service.py's sync_loan_notifications() (idempotent
INSERT OR IGNORE against a UNIQUE constraint, so re-running the sweep never
duplicates a notification that's already there).

(Bisaya: Kini nga file usa ka "sweep" - regular nga pag-check - para sa mga
asset nga due na sa maintenance, gipadagan sa iskedyul (tan-awa ang app.py).
Pareho ni sa sweep_overdue_members() sa loan_service.py, ug gigamit pod ang
parehas nga pamaagi sa notification_service.py's sync_loan_notifications():
"INSERT OR IGNORE" batok sa UNIQUE constraint, para bisan pila ka beses
mag-run ang sweep, dili na kini mag-duplicate sa notification nga naa na.)
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
    those two.
    (Bisaya: Mag-himo ug usa ka 'maintenance_due' notification para sa
    matag asset nga due na - o hapit na due sa sulod sa gi-set nga
    "lead time"/pasidaan nga adlaw - para sa sunod nga preventive
    maintenance, base sa petsa o base sa gidaghanon sa checkouts - tan-awa
    ang get_maintenance_due_assets. "Broadcast" ni nga notification (walay
    piho nga recipient_staff_id/recipient_role) - pareho ra sa due_soon ug
    overdue nga notification, makita ni sa TANANG naka-login nga staff sa
    ilang bell/notification icon.

    Walay parameter ug walay return value - kini gitawag lang sa iskedyul
    (background scheduler, tan-awa ang app.py), mag-abre kini sa iyang
    kaugalingong database connection ug mag-close human mahuman, dili ni
    mo-raise ug exception pataas - kung mapakyas, i-log ra kini.)"""
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
    """Today's date (localtime) as an ISO string, straight from SQLite so it
    matches whatever timezone the app's PRAGMAs/queries already assume.
    (Bisaya: Ang petsa karon - localtime - sa ISO format, gikan mismo sa
    SQLite para pareho gyud sa timezone nga gigamit sa uban pang parte sa
    app.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)

    Returns:
        str - e.g. "2026-09-08".
        (Bisaya: usa ka string, e.g. "2026-09-08".)
    """
    return conn.execute("SELECT date('now', 'localtime') AS d").fetchone()["d"]
