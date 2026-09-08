import re
from datetime import datetime
from flask import jsonify

ACCEPTED_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")  # single source of truth


def _parse_date(value):
    """Mobalik og datetime o None - internal ra, dili gamiton sa gawas
    ani nga file."""
    if value is None:
        return None
    for fmt in ACCEPTED_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def validate_date_string(value, field_name):
    """Format ra ang gina-check, dili kung nangagi na - gigamit sa
    assets/staff nga routes diin OK ra ang petsa nga nangagi. Blank
    string gitratar pareho "walay gipasa", dili "invalid".

    Returns: None kung valid/blank, or (jsonify({"error": ...}), 400)."""
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
    """I-run ang validate_date_string sa daghang field dungan. Field nga
    wala gani sa `data` gilaktawan gyud (missing != blank).

    Returns: ang UNA nga error nga makit-an, o None kung tanan valid."""
    for field in field_names:
        if field in data:
            error = validate_date_string(data.get(field), field)
            if error:
                return error
    return None


MIN_PASSWORD_LENGTH = 8


def validate_password_strength(password, field_name="password"):
    """Length-only ang gina-check karon; pwede pahigpiton unya kung
    kinahanglan mas lig-on nga policy.

    Returns: None kung igo na ka lig-on, or (jsonify({"error": ...}), 400)."""
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
    """Kinahanglan eksakto 6 ka numero - paspas i-type sa card reader,
    pero tinuod gihapon nga second factor (i-hash pareho sa password).

    Returns: None kung valid, or (jsonify({"error": ...}), 400)."""
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
    """I-parse/susihon ang ?limit= ug ?offset= - limit default 20, dili
    molapas sa 100 bisan unsaon pagsulay.

    Returns: tuple (limit, offset, error)."""
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
    """Susihon ang format UG kung wala pa nangagi - gi-parse kausa
    pinaagi sa _parse_date.

    Returns: None kung valid/wala pa nangagi, or (jsonify({"error": ...}), 400)."""
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
    """I-trim ug i-validate ang barcode_id nga gi-type/gi-scan sa staff.
    None nga normalized value (blank/wala gipasa) nagpasabot "walay
    barcode - mag-auto-generate na lang" - dili kini error.

    Returns: tuple (normalized_value_or_None, error_message_or_None)."""
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
    """Susihon kung naa gyud ang id_value sa `table` - `table` kinahanglan
    HARDCODED gyud, DILI gikan sa user input (gi-interpolate diretso sa
    SQL); ang _LOOKUP_TABLES allowlist mao ang proteksyon batok sa SQL
    injection diri.

    Returns: None kung valid, or (jsonify({"error": ...}), 400)."""
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


def escape_like_wildcards(value):
    """I-escape ang SQL LIKE wildcards (% ug _) sa usa ka user-typed search
    term, aron literal ang pagbasa niini (dili treated as wildcard) - e.g.
    search sa "50%" o "a_b" mo-match ra sa literal nga string, dili
    unexpected pattern. Gamiton dungan sa `LIKE ... ESCAPE '\\'` sa SQL -
    kung wala ang ESCAPE clause sa query, way epekto ni siya (mag-search
    ra sa literal backslash instead).

    Returns: str nga safe i-embed sa gawas sa '%...%' wrapper."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def decimal_scan_hex_suffix(code):
    """Pipila ka UHF reader, kung naka-set mag-type sa scan isip keyboard
    input (USB_HID), dili mag-type sa tibuok hex EPC - ang gina-type kay
    ang DECIMAL value sa katapusang 3 bytes/6 hex characters ra sa EPC.
    Confirmed sa real tag: EPC nga natapos sa hex E12345 scanned as decimal
    "14754629", ug int("14754629") == 0xE12345 exactly.

    A real hex EPC (naay A-F letters) dili gyud ma-match diri kay
    isdigit() mo-reject niini - mao nga safe ni i-OR sa laing query
    unconditionally (NULL LIKE anything = NULL = false, harmless).

    Returns: ang hex suffix (6 letra, uppercase), o None kung dili
    plausible nga decimal scan."""
    if not code or not code.isdigit():
        return None
    value = int(code)
    if value > 0xFFFFFF:  # doesn't fit in 3 bytes/24 bits
        return None
    return format(value, "06X")
