import re
from datetime import datetime
from flask import jsonify

ACCEPTED_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")  # single source of truth


def _parse_date(value):
    """Returns a datetime or None. Not exported - internal only."""
    if value is None:
        return None
    for fmt in ACCEPTED_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def validate_date_string(value, field_name):
    """Format-only check. Used by assets/staff routes where 'in the past' is fine.
    A blank string (an emptied date input, submitted as "" rather than omitted
    entirely) is treated the same as a missing field, not an invalid one -
    callers that actually require the field check that separately (e.g.
    reservations' own presence check before calling this)."""
    if value is None or value == "":
        return None
    if _parse_date(value) is None:
        return (
            jsonify(
                {
                    "error": f"{field_name} must be a valid date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)"
                }
            ),
            400,
        )
    return None


def validate_date_fields(data, field_names):
    """Runs validate_date_string over several fields at once.
    Returns the first error response found, or None if all are valid."""
    for field in field_names:
        if field in data:
            error = validate_date_string(data.get(field), field)
            if error:
                return error
    return None


MIN_PASSWORD_LENGTH = 8


def validate_password_strength(password, field_name="password"):
    """Length-only check for now; tighten later if a stronger policy is needed."""
    if password is None or len(password) < MIN_PASSWORD_LENGTH:
        return (
            jsonify(
                {
                    "error": f"{field_name} must be at least {MIN_PASSWORD_LENGTH} characters"
                }
            ),
            400,
        )
    return None


RFID_PIN_LENGTH = 6


def validate_rfid_pin(pin):
    """Exactly 6 digits - fast to type at a card reader, still a real second factor
    (hashed like a password, see routes/staff.py and routes/auth.py:login_rfid)."""
    if not pin or not pin.isdigit() or len(pin) != RFID_PIN_LENGTH:
        return (
            jsonify(
                {
                    "error": f"PIN must be exactly {RFID_PIN_LENGTH} digits"
                }
            ),
            400,
        )
    return None


DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100


def parse_pagination_params(args):
    """Parses/validates ?limit= and ?offset=; limit defaults to 20, capped at 100."""
    try:
        limit = int(args.get("limit", DEFAULT_PAGE_LIMIT))
    except ValueError:
        return None, None, (jsonify({"error": "limit must be an integer"}), 400)

    try:
        offset = int(args.get("offset", 0))
    except ValueError:
        return None, None, (jsonify({"error": "offset must be an integer"}), 400)

    if limit < 1:
        return None, None, (jsonify({"error": "limit must be at least 1"}), 400)
    if offset < 0:
        return None, None, (jsonify({"error": "offset cannot be negative"}), 400)

    limit = min(limit, MAX_PAGE_LIMIT)

    return limit, offset, None


def validate_due_date(value, field_name="due_date"):
    """Format + not-in-the-past, parsing once via _parse_date."""
    if value is None:
        return None
    parsed = _parse_date(value)
    if parsed is None:
        return (
            jsonify(
                {
                    "error": f"{field_name} must be a valid date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)"
                }
            ),
            400,
        )
    if parsed < datetime.now():
        return (
            jsonify({"error": f"{field_name} cannot be in the past"}),
            400,
        )
    return None


MAX_BARCODE_LENGTH = 64
_BARCODE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def normalize_barcode_id(raw_value):
    """Trims/validates a staff-supplied barcode_id (e.g. scanned off an existing ID card).

    Returns (normalized_value_or_None, error_message_or_None). A None normalized
    value (blank/omitted input) means "no barcode supplied - caller should
    auto-generate one", not an error. Framework-agnostic on purpose so both
    Flask routes and services can wrap the error message in their own response
    shape (services use plain {"error": ...} dicts, not jsonify).
    """
    if raw_value is None:
        return None, None
    value = raw_value.strip()
    if not value:
        return None, None
    if len(value) > MAX_BARCODE_LENGTH:
        return None, f"barcode_id must be at most {MAX_BARCODE_LENGTH} characters"
    if not _BARCODE_ID_RE.match(value):
        return None, "barcode_id may only contain letters, numbers, hyphens, underscores, and periods"
    return value, None


# `table` is interpolated into SQL; this allowlist is the real safety net.
_LOOKUP_TABLES = {"departments", "categories"}


def validate_lookup_id(conn, table, id_value, field_name, required=False):
    """Checks id_value exists in `table`; table must always be hardcoded, never user input."""
    if table not in _LOOKUP_TABLES:
        raise ValueError(
            f"validate_lookup_id: {table!r} is not an allowed lookup table "
            f"(add it to _LOOKUP_TABLES in utils/validators.py if this is intentional)"
        )

    if id_value is None:
        if required:
            return jsonify({"error": f"Missing required field: {field_name}"}), 400
        return None

    row = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (id_value,)).fetchone()
    if row is None:
        return (
            jsonify({"error": f"{field_name} does not reference an existing entry"}),
            400,
        )
    return None


def decimal_scan_hex_suffix(code):
    """Some UHF readers, when set to type scans as keyboard input (USB_HID),
    don't type the full hex EPC - they type the DECIMAL value of just the
    EPC's last 3 bytes (6 hex characters), access-control-Wiegand style.
    Confirmed against a real tag: EPC ...ABCDE12345 (ends in hex E12345)
    scanned as the decimal digits "14754629", and int("14754629") ==
    0xE12345 exactly - see the project conversation history for the
    reasoning/derivation.

    Returns that hex suffix (uppercase, zero-padded to 6 characters) so a
    caller can ALSO match a scanned code against it, or None if `code`
    isn't a plausible decimal scan (empty, has non-digit characters, or
    too large to be 3 bytes) - callers can bind this straight into an
    `OR UPPER(x) LIKE '%' || ?` clause unconditionally, since SQLite
    string-concatenating NULL with anything yields NULL, making that
    OR-branch harmlessly false rather than needing a separate query.

    A real hex EPC (e.g. "E2003411...") never reaches here since it
    contains A-F letters and isdigit() correctly rejects it - this only
    ever fires for something that's ALL digits, i.e. plausibly one of
    these decimal-truncated scans in the first place."""
    if not code or not code.isdigit():
        return None
    value = int(code)
    if value > 0xFFFFFF:  # doesn't fit in 3 bytes/24 bits
        return None
    return format(value, "06X")
