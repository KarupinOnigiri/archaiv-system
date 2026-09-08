"""App-wide configurable settings (session timing, lockout, feature flags,
barcode prefixes, etc) - tanan gi-store sa usa ra ka key/value nga
app_settings table. Kung wala pa ang row, mo-fallback ra sa hardcoded
default dinhi, para dili mag-crash ang bag-o/wala pa na-migrate nga install.
"""

from database import get_db_connection

# Sentinel for "locked with no expiry" in staff.locked_until, without a schema change.
INDEFINITE_LOCK_UNTIL = "9999-12-31 23:59:59"

# Same sentinel idea, for assigned_end - lets an assignment be "Permanent" without a schema change.
PERMANENT_ASSIGNED_END = "9999-12-31"

# Fallback only - real numbers live in app_settings; a missing row shouldn't crash a login.
DEFAULT_SETTINGS = {
    "session_idle_minutes": 30,
    "session_absolute_hours": 12,
    "max_failed_attempts": 5,
    "lockout_minutes": 15,
    "due_soon_hours": 24,
    "default_loan_days": 2,
    "damage_restriction_threshold": 2,
    "auto_backup_interval_hours": 24,
    "archive_purge_days": 0,  # '0' means "None" - auto-purge disabled.
    # How long an in-progress checkout cart (dock, not a staff login session)
    # can sit untouched before it's auto-cancelled and its reserved assets
    # released - matches the original hardcoded utils/constants.py value.
    "checkout_session_stale_minutes": 20,
    # How many days ahead of an asset's next_maintenance_due (migration
    # 0055) counts as "due soon" for the dashboard card and the
    # maintenance_due notification sweep.
    "maintenance_reminder_lead_days": 7,
    # Default interval (days) used to auto-fill a new asset's
    # next_maintenance_due when none is set by hand - see
    # services/asset_service.py's _default_next_maintenance_due (migration 0059).
    "default_maintenance_interval_days": 180,
}

# Feature flags default to enabled; a missing row should never silently lock one out.
# monitoring is the deliberate exception - it depends on RFID hardware that
# isn't necessarily deployed yet, so both its seeded row (migration 0048) and
# this fallback default to disabled instead.
DEFAULT_FEATURE_FLAGS = {
    "feature_assets_enabled": True,
    "feature_members_enabled": True,
    "feature_transactions_enabled": True,
    "feature_requests_enabled": True,
    "feature_reports_enabled": True,
    "feature_monitoring_enabled": False,
    # Off by default, same as monitoring - opt-in via System settings once
    # staff are ready to use advance booking (migration 0057 seeds the row).
    "feature_reservations_enabled": False,
}

# Short name -> (app_settings key, human label for disabled/redirect messages).
FEATURE_FLAGS = {
    "assets": ("feature_assets_enabled", "Inventory"),
    "members": ("feature_members_enabled", "Members"),
    "transactions": ("feature_transactions_enabled", "Borrow & Return"),
    "requests": ("feature_requests_enabled", "Approvals"),
    "reports": ("feature_reports_enabled", "Reports"),
    "monitoring": ("feature_monitoring_enabled", "Monitoring"),
    "reservations": ("feature_reservations_enabled", "Reservations"),
}


def get_setting_int(key, conn=None):
    """Pangitaon ang numero nga setting base sa key, mo-fallback sa
    DEFAULT_SETTINGS kung wala/dili ma-parse. Gigamit sa tanang get_X_*
    nga mubo nga wrapper sa ubos.

    Returns: int."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return DEFAULT_SETTINGS[key]
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return DEFAULT_SETTINGS[key]
    finally:
        if owns_conn:
            conn.close()


def get_session_idle_minutes(conn=None):
    """Pila ka minuto nga walay lihok sa dili pa mag-expire ang session.
    Returns: int."""
    return get_setting_int("session_idle_minutes", conn)


def get_session_absolute_hours(conn=None):
    """Pinakataas nga oras nga mabuhi ang session bisan naay padayon nga
    lihok. Returns: int."""
    return get_setting_int("session_absolute_hours", conn)


def get_max_failed_attempts(conn=None):
    """Pila ka sayop nga pagsulay sa login/PIN nga gitugotan sa dili pa
    mag-lock ang account. Returns: int."""
    return get_setting_int("max_failed_attempts", conn)


def get_lockout_minutes(conn=None):
    """Pila ka minuto nga naka-lock ang account human maabot ang
    max_failed_attempts. Returns: int."""
    return get_setting_int("lockout_minutes", conn)


def get_due_soon_hours(conn=None):
    """Pila ka oras sa dili pa moabot ang due date nga gina-isip na nga
    "due soon". Returns: int."""
    return get_setting_int("due_soon_hours", conn)


def get_default_loan_days(conn=None):
    """Default nga gidugayon (sa adlaw) sa loan para sa bag-ong checkout.
    Returns: int."""
    return get_setting_int("default_loan_days", conn)


def get_damage_restriction_threshold(conn=None):
    """Pila ka beses ka-balik nga naay damage sa dili pa ma-auto-restrict
    ang member. Returns: int."""
    return get_setting_int("damage_restriction_threshold", conn)


def get_auto_backup_interval_hours(conn=None):
    """Pila ka oras ang gilay-on sa matag automatic backup sa database.
    Returns: int."""
    return get_setting_int("auto_backup_interval_hours", conn)


def get_archive_purge_days(conn=None):
    """0 nagpasabot "None" (disabled ang auto-purge); kung dili, pila ka
    adlaw human sa deleted_at/deactivated_at. Returns: int."""
    return get_setting_int("archive_purge_days", conn)


# Same preset day values as templates/staff/_archive_purge_settings_modal.html's
# own picker - kept in sync by hand since one's a Jinja loop and the other a
# plain label, not worth a shared data source for 5 entries.
ARCHIVE_PURGE_PRESET_LABELS = {0: "Off", 7: "1 week", 30: "1 month", 180: "6 months", 365: "1 year"}


def format_archive_purge_label(days):
    """Mubo nga label para sa summary line sa Auto-Delete card sa System
    page. Returns: str, e.g. "1 month"."""
    return ARCHIVE_PURGE_PRESET_LABELS.get(days, f"{days} days")


def get_default_maintenance_interval_days(conn=None):
    """Default nga pila ka adlaw sa unahan ang next_maintenance_due sa
    bag-o/na-serbisyohan nga asset kung wala ni-set ang staff mismo.
    Returns: int."""
    return get_setting_int("default_maintenance_interval_days", conn)


def get_maintenance_reminder_lead_days(conn=None):
    """Pila ka adlaw sa unahan sa next_maintenance_due (o pila na ka duol
    sa maintenance_interval_checkouts) ang gina-isip nga "due soon".
    Returns: int."""
    return get_setting_int("maintenance_reminder_lead_days", conn)


def get_checkout_session_stale_minutes(conn=None):
    """Pila ka minuto nga wala hilabti ang in-progress checkout cart
    (gi-dock ug nalimtan, o na-close ang tab) sa dili pa auto-cancel-on
    ug buhian ang reserved assets. Returns: int."""
    return get_setting_int("checkout_session_stale_minutes", conn)


def get_setting_bool(key, conn=None, default=True):
    """Pareho ka porma sa get_setting_int, para sa '1'/'0' nga row
    (boolean). Ang `default` importante ra kung wala sa
    DEFAULT_FEATURE_FLAGS ang key - tugotan ang laing boolean setting nga
    naay kaugalingong fallback nga dili na idugang sa maong dict.

    Returns: bool."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return DEFAULT_FEATURE_FLAGS.get(key, default)
        return row["value"] == "1"
    finally:
        if owns_conn:
            conn.close()


def get_auto_backup_enabled(conn=None):
    """Mo-fallback sa True by default, pareho sa safety-first nga default
    sa feature flags. Returns: bool."""
    return get_setting_bool("auto_backup_enabled", conn)


def get_alarm_enabled(conn=None):
    """Kung mo-raise gyud ba ang alarm (banner, active-alarm state) sa
    usa ka unauthorized/unknown-tag nga RFID detection. Gi-classify ug
    gi-log gihapon ang detection bisan unsaon - kini ra ang mo-gate sa
    alarm mismo, lahi sa "monitoring" feature flag.

    Returns: bool."""
    return get_setting_bool("alarm_enabled", conn)


def get_setting_str(key, conn=None, default=""):
    """Bersyon para sa string sa get_setting_int/get_setting_bool - para
    sa mga setting sama sa barcode ID prefixes nga dili numero/flag.

    Returns: str - ang na-store nga value, o `default`."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None or not row["value"]:
            return default
        return row["value"]
    finally:
        if owns_conn:
            conn.close()


# Prefix defaults match the values every asset_id/member_code/staff_code was
# hardcoded to before this setting existed (migration 0052 seeds the same),
# so an unmigrated/missing row changes nothing behaviorally.
DEFAULT_ASSET_ID_PREFIX = "AST"
DEFAULT_MEMBER_CODE_PREFIX = "MBR"
DEFAULT_STAFF_CODE_PREFIX = "STF"

# 2-6 chars, uppercase letters/digits plus a handful of common label symbols
# (- _ . #) - short enough to keep "AST00042"-style codes compact, long
# enough to be a recognizable label; letters-or-digits (not letters-only)
# leaves room for something like "A1" or "TR1" if a school wants to encode
# more than one category. No cross-prefix uniqueness requirement - assets/
# members/staff are three separate tables and columns, so even identical
# prefixes across them can never collide with each other.
#
# '/' is deliberately excluded even though it's a common label separator
# elsewhere: barcode_id/asset_id/member_code/staff_code values flow straight
# into URL path segments (e.g. GET /api/v1/assets/barcode/<barcode>), where
# a literal slash would split into two path segments and 404 instead of
# reaching the route at all.
BARCODE_PREFIX_PATTERN = r"^[A-Z0-9\-_.#]{2,6}$"


def get_asset_id_prefix(conn=None):
    """Ang prefix nga gigamit sa bag-ong asset_id code, e.g. "AST" para sa
    "AST00042". Returns: str."""
    return get_setting_str("asset_id_prefix", conn, DEFAULT_ASSET_ID_PREFIX)


def get_member_code_prefix(conn=None):
    """Ang prefix nga gigamit sa bag-ong member_code, e.g. "MBR".
    Returns: str."""
    return get_setting_str("member_code_prefix", conn, DEFAULT_MEMBER_CODE_PREFIX)


def get_staff_code_prefix(conn=None):
    """Ang prefix nga gigamit sa bag-ong staff_code, e.g. "STF".
    Returns: str."""
    return get_setting_str("staff_code_prefix", conn, DEFAULT_STAFF_CODE_PREFIX)


def get_feature_flags(conn=None):
    """Mobalik ug {name: enabled} para sa matag entry sa FEATURE_FLAGS -
    gigamit sa templates (e.g. sidebar nav) ug sa route gating.

    Returns: dict {str: bool}."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        return {
            name: get_setting_bool(key, conn)
            for name, (key, _label) in FEATURE_FLAGS.items()
        }
    finally:
        if owns_conn:
            conn.close()


def is_feature_enabled(name, conn=None):
    """Mubo nga wrapper alang sa usa ra ka flag - gamiton kung usa ra ka
    feature ang gusto susihon, dili tanan.

    Returns: bool - True kung naka-enable."""
    key, _label = FEATURE_FLAGS[name]
    return get_setting_bool(key, conn)


def get_feature_label(name):
    """Ngalan nga sayon basahon para sa usa ka feature flag, e.g. "assets"
    -> "Inventory" - gigamit sa "X is currently disabled" nga mensahe.

    Returns: str."""
    return FEATURE_FLAGS[name][1]
