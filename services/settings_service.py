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
    """Looks up an int setting by key, falling back to DEFAULT_SETTINGS if missing/unparseable."""
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
    return get_setting_int("session_idle_minutes", conn)


def get_session_absolute_hours(conn=None):
    return get_setting_int("session_absolute_hours", conn)


def get_max_failed_attempts(conn=None):
    return get_setting_int("max_failed_attempts", conn)


def get_lockout_minutes(conn=None):
    return get_setting_int("lockout_minutes", conn)


def get_due_soon_hours(conn=None):
    return get_setting_int("due_soon_hours", conn)


def get_default_loan_days(conn=None):
    return get_setting_int("default_loan_days", conn)


def get_damage_restriction_threshold(conn=None):
    return get_setting_int("damage_restriction_threshold", conn)


def get_auto_backup_interval_hours(conn=None):
    return get_setting_int("auto_backup_interval_hours", conn)


def get_archive_purge_days(conn=None):
    """0 means "None" - auto-purge disabled; otherwise days since deleted_at/deactivated_at."""
    return get_setting_int("archive_purge_days", conn)


# Same preset day values as templates/staff/_archive_purge_settings_modal.html's
# own picker - kept in sync by hand since one's a Jinja loop and the other a
# plain label, not worth a shared data source for 5 entries.
ARCHIVE_PURGE_PRESET_LABELS = {0: "Off", 7: "1 week", 30: "1 month", 180: "6 months", 365: "1 year"}


def format_archive_purge_label(days):
    """Short label for the System page's Auto-Delete card summary line."""
    return ARCHIVE_PURGE_PRESET_LABELS.get(days, f"{days} days")


def get_default_maintenance_interval_days(conn=None):
    """Default days-out for a new/re-serviced asset's next_maintenance_due
    when staff don't set one explicitly - see asset_service.py's
    _default_next_maintenance_due."""
    return get_setting_int("default_maintenance_interval_days", conn)


def get_maintenance_reminder_lead_days(conn=None):
    """How many days ahead of next_maintenance_due (or how close to
    maintenance_interval_checkouts) counts as "due soon" - services/
    maintenance_service.py's scheduled sweep, and get_asset_stat_cards'
    low_stock-style due-soon count."""
    return get_setting_int("maintenance_reminder_lead_days", conn)


def get_checkout_session_stale_minutes(conn=None):
    """How long an in-progress checkout cart can sit untouched (docked and
    forgotten, or the browser tab just closed) before it's auto-cancelled
    and its reserved assets released - services/checkout_service.py's
    scheduled sweep, and the lazy same-check done inline whenever a session
    is looked up (services/transaction_service.py)."""
    return get_setting_int("checkout_session_stale_minutes", conn)


def get_setting_bool(key, conn=None, default=True):
    """Same fallback pattern as get_setting_int, for '1'/'0' rows. `default` only
    matters for a key with no DEFAULT_FEATURE_FLAGS entry of its own (every
    feature flag keeps defaulting the way DEFAULT_FEATURE_FLAGS already says,
    regardless of what's passed here) - lets a non-tab boolean setting (e.g.
    alarm_enabled) declare its own fallback without being added to that dict,
    which is specifically about tab-gating flags and their redirect labels."""
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
    """Falls back to True by default, like the feature flags' own safety-first default."""
    return get_setting_bool("auto_backup_enabled", conn)


def get_alarm_enabled(conn=None):
    """Whether an unauthorized/unknown-tag RFID detection should actually raise
    the alarm (banner, active-alarm state) - see services/monitoring_service.py's
    record_rfid_event. Detections are still classified and logged either way;
    this only gates the alarm itself, independent of the "monitoring" feature
    flag (which gates the whole tab/hardware pipeline, not just this)."""
    return get_setting_bool("alarm_enabled", conn)


def get_setting_str(key, conn=None, default=""):
    """String counterpart to get_setting_int/get_setting_bool - for settings
    like the barcode ID prefixes that aren't numbers or on/off flags."""
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
    return get_setting_str("asset_id_prefix", conn, DEFAULT_ASSET_ID_PREFIX)


def get_member_code_prefix(conn=None):
    return get_setting_str("member_code_prefix", conn, DEFAULT_MEMBER_CODE_PREFIX)


def get_staff_code_prefix(conn=None):
    return get_setting_str("staff_code_prefix", conn, DEFAULT_STAFF_CODE_PREFIX)


def get_feature_flags(conn=None):
    """Returns {name: enabled} for every FEATURE_FLAGS entry; used by templates and route gating."""
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
    """Single-flag convenience wrapper around get_feature_flags for callers checking just one."""
    key, _label = FEATURE_FLAGS[name]
    return get_setting_bool(key, conn)


def get_feature_label(name):
    return FEATURE_FLAGS[name][1]
