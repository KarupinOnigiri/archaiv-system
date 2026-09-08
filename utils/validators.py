import re
from datetime import datetime
from flask import jsonify

ACCEPTED_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")  # single source of truth


def _parse_date(value):
    """Returns a datetime or None. Not exported - internal only.
    (Bisaya: Mobalik ug datetime o None. Dili ni gamiton sa gawas sa niini
    nga file - internal ra.)

    Args:
        value: a date string in "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS"
            format, or None.
        (Bisaya: usa ka string sa petsa - format "YYYY-MM-DD" o
        "YYYY-MM-DD HH:MM:SS" - o None.)

    Returns:
        datetime or None - None when value is None or doesn't match either
        accepted format.
        (Bisaya: datetime o None - None kung walay laman ang value o dili
        ni motukma sa gina-dawat nga format.)
    """
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
    reservations' own presence check before calling this).
    (Bisaya: Format ra ang gina-check, dili kung nangagi na - gigamit sa
    assets/staff nga mga route diin OK ra ang petsa nga nangagi na. Kung
    walay laman nga string - gi-clear ang date input, gipadala isip "" imbes
    wala gyud gipasa - gitratar pareho sa "walay gipasa", dili "invalid" -
    ang mga nagtawag nga kinahanglan gyud ang field ang mo-check anang
    separado, e.g. reservations own presence check una tawagon ni.)

    Args:
        value: the date string to check, or None/"" (both pass).
        (Bisaya: ang string sa petsa nga susihon, o None/"" - pareho ni
        pasaron.)
        field_name: label used in the error message, e.g. "acquired_date".
        (Bisaya: ngalan sa field, gigamit sa error message, e.g.
        "acquired_date".)

    Returns:
        None when valid (or blank), or a (jsonify({"error": ...}), 400)
        tuple ready to return straight from a Flask route when invalid.
        (Bisaya: None kung valid - o walay laman - o usa ka
        (jsonify({"error": ...}), 400) nga tuple, andam na i-return diretso
        gikan sa Flask route kung dili valid.)
    """
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
    Returns the first error response found, or None if all are valid.
    (Bisaya: I-run/pahigayonon ang validate_date_string sa daghang field
    dungan. Ibalik ang UNA nga error nga makit-an, o None kung tanan valid.)

    Args:
        data: dict-like request payload (e.g. request.form or a parsed
            JSON body).
        (Bisaya: dict-like nga payload sa request, e.g. request.form o
        na-parse nga JSON body.)
        field_names: iterable of field names to check, skipped entirely if
            not present in `data` at all (missing != blank, see
            validate_date_string).
        (Bisaya: listahan sa mga field nga susihon - laktawan gyud kung
        wala gani sila sa `data` - lahi ang "wala" ug "walay laman", tan-awa
        ang validate_date_string.)

    Returns:
        None if every present field is valid, otherwise the first
        (jsonify({"error": ...}), 400) tuple encountered.
        (Bisaya: None kung valid ang tanan nga naa - kung dili, ang UNA nga
        (jsonify({"error": ...}), 400) nga tuple.)
    """
    for field in field_names:
        if field in data:
            error = validate_date_string(data.get(field), field)
            if error:
                return error
    return None


MIN_PASSWORD_LENGTH = 8


def validate_password_strength(password, field_name="password"):
    """Length-only check for now; tighten later if a stronger policy is needed.
    (Bisaya: Gidak-on/length ra ang gina-check karon - pwede pahigpiton
    unya kung kinahanglan mas lig-on nga password policy.)

    Args:
        password: the plain-text password to check (never logged/stored
            as-is - hashing happens elsewhere, this only checks strength).
        (Bisaya: ang password mismo nga i-check - dili gyud kini i-log o
        i-store nga plain-text, gihash ni sa lain nga bahin sa code, dinhi
        ang gidak-on/length ra ang gisusi.)
        field_name: label used in the error message, defaults to
            "password".
        (Bisaya: ngalan sa field para sa error message, default
        "password".)

    Returns:
        None when strong enough, or a (jsonify({"error": ...}), 400) tuple
        otherwise.
        (Bisaya: None kung igo na ka lig-on, o (jsonify({"error": ...}),
        400) kung dili.)
    """
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
    (hashed like a password, see routes/staff.py and routes/auth.py:login_rfid).
    (Bisaya: Kinahanglan eksakto gyud 6 ka numero - paspas i-type sa card
    reader, pero tinuod gihapon nga "second factor" - i-hash pareho sa
    password, tan-awa ang routes/staff.py ug routes/auth.py:login_rfid.)

    Args:
        pin: the PIN string to check.
        (Bisaya: ang PIN nga susihon.)

    Returns:
        None when valid, or a (jsonify({"error": ...}), 400) tuple when
        it's missing, not all digits, or the wrong length.
        (Bisaya: None kung valid, o (jsonify({"error": ...}), 400) kung
        walay laman, dili tanan numero, o sayop ang gidak-on.)
    """
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
    """Parses/validates ?limit= and ?offset=; limit defaults to 20, capped at 100.
    (Bisaya: I-parse/susihon ang ?limit= ug ?offset= sa URL query string -
    ang limit default 20, dili molapas sa 100 bisan unsaon pagsulay.)

    Args:
        args: a dict-like object with .get(), e.g. Flask's request.args.
        (Bisaya: usa ka dict-like nga object nga naay .get(), e.g. Flask's
        request.args.)

    Returns:
        tuple (limit, offset, error) - on success, `limit` (int, 1-100)
        and `offset` (int, >= 0) are set and `error` is None; on failure,
        both are None and `error` is a (jsonify({"error": ...}), 400)
        tuple.
        (Bisaya: usa ka tuple (limit, offset, error) - kung malampuson,
        naay value ang `limit` (1-100) ug `offset` (0 pataas), walay laman
        ang `error`; kung napakyas, walay laman ang duha, ug ang `error`
        naay (jsonify({"error": ...}), 400) nga tuple.)
    """
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
    """Format + not-in-the-past, parsing once via _parse_date.
    (Bisaya: Susihon ang format UG kung wala pa ni molabay/nangagi -
    gi-parse ra kausa pinaagi sa _parse_date.)

    Args:
        value: the date string to check, or None (passes).
        (Bisaya: ang string sa petsa nga susihon, o None - pasaron ni.)
        field_name: label used in the error message, defaults to "due_date".
        (Bisaya: ngalan sa field para sa error message, default
        "due_date".)

    Returns:
        None when valid and not in the past, otherwise a
        (jsonify({"error": ...}), 400) tuple.
        (Bisaya: None kung valid ug wala pa nangagi, kung dili,
        (jsonify({"error": ...}), 400) nga tuple.)
    """
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
    (Bisaya: I-trim/hawanan ug i-validate ang barcode_id nga gi-type/gi-scan
    sa staff - e.g. gi-scan gikan sa naa nay ID card.)

    Args:
        raw_value: the raw barcode_id text from the form/request, or None.
        (Bisaya: ang hilaw/raw nga barcode_id gikan sa form/request, o
        None.)

    Returns:
        tuple (normalized_value_or_None, error_message_or_None). A None
        normalized value (blank/omitted input) means "no barcode supplied -
        caller should auto-generate one", not an error. Framework-agnostic
        on purpose so both Flask routes and services can wrap the error
        message in their own response shape (services use plain
        {"error": ...} dicts, not jsonify).
        (Bisaya: usa ka tuple (normalized_value_or_None,
        error_message_or_None). Kung None ang normalized value - walay
        laman/wala gipasa - kini nagpasabot nga "walay barcode nga gipasa -
        kinahanglan mag-auto-generate na lang ang nagtawag", dili kini
        error. Wala ni gi-bind sa Flask mismo, tuyo gyud, para pareho ang
        Flask routes ug services makagamit sa error message sa ilang
        kaugalingong porma - ang services plain {"error": ...} dict ra ang
        gamit, dili jsonify.)
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
    """Checks id_value exists in `table`; table must always be hardcoded, never user input.
    (Bisaya: Susihon kung naa gyud ang id_value sa `table` - ang `table`
    kinahanglan HARDCODED gyud, DILI GYUD gikan sa user input - kay
    gi-interpolate kini diretso sa SQL string; ang _LOOKUP_TABLES allowlist
    sa taas mao ang tinuod nga proteksyon batok sa SQL injection dinhi.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        table: the lookup table name - MUST be a hardcoded literal from
            the caller's own code, checked against _LOOKUP_TABLES above;
            raises ValueError (a programming bug, not a user-facing error)
            if it isn't allowlisted.
        (Bisaya: ngalan sa lookup table - KINAHANGLAN gikan sa hardcoded
        nga code sa nagtawag, gi-susi batok sa _LOOKUP_TABLES sa taas;
        mag-raise ug ValueError - sayop sa code, dili para sa user - kung
        wala kini sa allowlist.)
        id_value: the id to check exists, or None.
        (Bisaya: ang id nga susihon kung naa ba gyud, o None.)
        field_name: label used in the error message.
        (Bisaya: ngalan sa field para sa error message.)
        required: when True, a None id_value is itself an error ("missing
            required field"); when False (default), None just passes.
        (Bisaya: kung True, error na ang None nga id_value mismo - "missing
        required field"; kung False (default), pasaron ra ang None.)

    Returns:
        None when valid, or a (jsonify({"error": ...}), 400) tuple when
        the id is missing-but-required, or doesn't exist in `table`.
        (Bisaya: None kung valid, o (jsonify({"error": ...}), 400) nga
        tuple kung walay laman apan required, o wala diay ni nga id sa
        `table`.)
    """
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
    these decimal-truncated scans in the first place.

    (Bisaya: Pipila ka UHF reader, kung naka-set nga mag-type sa scan isip
    keyboard input (USB_HID), dili gyud i-type ang tibuok hex EPC - ang
    gina-type kay ang DECIMAL nga value sa katapusang 3 bytes/6 hex
    character ra sa EPC. Kini nga function mo-convert balik sa hex kung
    plausible/posible nga usa ni sa maong klase nga decimal scan.

    Args:
        code: ang na-scan nga code, tan-awon kung tanan numero ba.

    Returns:
        Ang hex suffix (6 letra, uppercase), o None kung dili plausible
        nga decimal scan ni.)
    """
    if not code or not code.isdigit():
        return None
    value = int(code)
    if value > 0xFFFFFF:  # doesn't fit in 3 bytes/24 bits
        return None
    return format(value, "06X")
