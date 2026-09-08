"""Scheduled maintenance-due sweep - usa ka regular nga pag-check para sa
mga asset nga due na sa maintenance, gipadagan sa iskedyul (app.py).
Pareho ni sa loan_service.py's sweep_overdue_members(), gigamit pod ang
"INSERT OR IGNORE" batok sa UNIQUE constraint aron dili ma-duplicate ang
notification bisan pila ka beses mag-run.
"""

import logging

from database import get_db_connection
from services.asset_service import get_maintenance_due_assets
from services.settings_service import get_maintenance_reminder_lead_days

logger = logging.getLogger(__name__)


def sweep_maintenance_due():
    """Mag-himo og 'maintenance_due' notification per asset nga due na (o
    hapit na due) sa preventive maintenance - date-based o
    usage-interval-based, tan-awa ang get_maintenance_due_assets.
    Broadcast ni - makita sa TANANG naka-login nga staff sa ilang bell.

    Walay parameter/return value - gitawag sa scheduler, naay kaugalingong
    connection; mapakyas man i-log ra."""
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
    """Petsa karon (localtime) as ISO string, gikan mismo sa SQLite para
    pareho gyud sa timezone nga gigamit sa app.

    Returns: str, e.g. "2026-09-08"."""
    return conn.execute("SELECT date('now', 'localtime') AS d").fetchone()["d"]
