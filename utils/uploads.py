import os
import secrets
import logging
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # matches the add-asset form's upload placeholder

# Served via static/uploads/assets/, same as any other static asset - no separate route needed.
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
ASSET_IMAGE_SUBDIR = os.path.join("uploads", "assets")
ASSET_IMAGE_DIR = os.path.join(STATIC_DIR, ASSET_IMAGE_SUBDIR)

# Own subdir, separate lifecycle from asset images - avoids filename collisions.
STAFF_PHOTO_SUBDIR = os.path.join("uploads", "staff")
STAFF_PHOTO_DIR = os.path.join(STATIC_DIR, STAFF_PHOTO_SUBDIR)

# Own subdir too - zero-or-more per feedback report, never replaced in place.
FEEDBACK_IMAGE_SUBDIR = os.path.join("uploads", "feedback")
FEEDBACK_IMAGE_DIR = os.path.join(STATIC_DIR, FEEDBACK_IMAGE_SUBDIR)

# Cap per report on top of the per-file MAX_IMAGE_SIZE_BYTES cap - more than this is abuse.
MAX_FEEDBACK_IMAGES = 5

# Downscale caps in px (longest side); nothing here ever displays a photo this large.
ASSET_IMAGE_MAX_DIMENSION = 1600
STAFF_PHOTO_MAX_DIMENSION = 800
FEEDBACK_IMAGE_MAX_DIMENSION = 1920


def _save_resized_image(file_storage, absolute_path, ext, max_dimension):
    """Downscales and re-saves the upload; falls back to a plain save on any Pillow error.
    (Bisaya: I-gamay/downscale ug i-save pag-usab ang gi-upload nga hulagway
    - kung mapakyas ang Pillow sa bisan unsang rason, mo-fallback ra sa
    plain/tinuod nga save nga wala ni-resize, dili gyud mabiyaan/mawala
    ang na-upload.)

    Args:
        file_storage: Werkzeug FileStorage - the raw uploaded file, already
            passed validate_image_upload().
        (Bisaya: Werkzeug FileStorage - ang hilaw/raw nga na-upload nga
        file, na-validate na sa validate_image_upload().)
        absolute_path: full filesystem path to write the final image to.
        (Bisaya: kompleto nga path sa disk kung asa i-save ang final nga
        hulagway.)
        ext: lowercase extension without the dot (e.g. "jpg"), decides the
            save format/quality settings.
        (Bisaya: extension sa file nga gamay letra, walay tuldok, e.g.
        "jpg" - kini ang mo-desisyon sa format/quality nga gamiton.)
        max_dimension: longest-side cap in pixels; the image is
            downscaled to fit within this while keeping its aspect ratio.
        (Bisaya: pinakataas nga gidak-on sa px sa pinakataas nga sidog -
        gamayon ang hulagway aron mosuksok niini, apan dili mausab ang
        proporsyon/aspect ratio.)

    Returns:
        None - writes directly to absolute_path; errors are logged, not
        raised (see the fallback above).
        (Bisaya: Walay gibalik - direkta ni mosuwat sa absolute_path; ang
        mga error i-log ra, dili i-raise pataas.)
    """
    try:
        file_storage.stream.seek(0)
        with Image.open(file_storage.stream) as img:
            img.load()  # decode now, while the stream is still open
            # JPEG has no alpha channel - P/RGBA/LA images need flattening first or save fails.
            if ext in ("jpg", "jpeg") and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
            save_kwargs = (
                {"quality": 85, "optimize": True}
                if ext in ("jpg", "jpeg")
                else {"optimize": True}
            )
            img.save(absolute_path, **save_kwargs)
    except Exception as e:
        logger.warning(f"Image resize failed for {absolute_path}, saving original: {e}")
        file_storage.stream.seek(0)
        file_storage.save(absolute_path)
    finally:
        file_storage.stream.seek(0)


def _extension(filename):
    """Lowercased file extension without the dot, or None if there isn't one.
    (Bisaya: Ang extension sa file, gamay letra, walay tuldok - None kung
    walay extension.)

    Args:
        filename: the original uploaded filename (may be None/empty).
        (Bisaya: ang orihinal nga filename sa na-upload, pwede walay
        laman/None.)

    Returns:
        str or None - e.g. "png", or None when there's no "." in filename.
        (Bisaya: usa ka string, e.g. "png", o None kung walay tuldok/"."
        sa filename.)
    """
    if not filename or "." not in filename:
        return None
    return filename.rsplit(".", 1)[1].lower()


def validate_image_upload(file_storage):
    """Checks extension, size, and actually decodes the file to confirm it's
    a real image - not just relying on the filename/extension, which is
    trivial to fake. Shared by every save_*() below, so every upload path
    (asset photo, staff photo, feedback screenshot) gets the same checks.
    (Bisaya: Susihon ang extension, ang size, ug tinuod nga i-decode ang
    file para makumpirma nga tinuod gyud ni nga hulagway - dili lang
    mosalig sa filename/extension, kay sayon ra ni palsohon. Gigamit sa
    matag save_*() sa ubos, para pareho gyud nga check ang gina-agian sa
    tanang upload - asset photo, staff photo, feedback screenshot.)

    Args:
        file_storage: Werkzeug FileStorage from the form upload, or None.
        (Bisaya: Werkzeug FileStorage gikan sa form upload, o None.)

    Returns:
        str or None - an error message describing why the upload is
        rejected, or None when it passed every check (empty file, no
        extension, wrong extension, too big, or fails to actually decode
        as an image).
        (Bisaya: usa ka string nga error message kung ngano gi-reject ang
        upload, o None kung nipasar sa tanang check - walay laman nga
        file, walay extension, sayop nga extension, sobra ka dako, o
        dili gyud ma-decode isip tinuod nga hulagway.)
    """
    if file_storage is None or file_storage.filename == "":
        return "No image file provided"

    ext = _extension(file_storage.filename)
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return f"Image must be one of: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}"

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)

    if size == 0:
        return "Uploaded image is empty"
    if size > MAX_IMAGE_SIZE_BYTES:
        return f"Image must be under {MAX_IMAGE_SIZE_BYTES // (1024 * 1024)}MB"

    try:
        with Image.open(file_storage.stream) as img:
            img.verify()
    # SyntaxError too - Pillow's PNG decoder raises it for corrupt chunk data.
    except (UnidentifiedImageError, OSError, SyntaxError):
        return "File is not a valid image"
    finally:
        file_storage.stream.seek(0)

    return None


def save_asset_image(file_storage, asset_code):
    """Validates and saves an asset image; returns (relative_path, error) for assets.image_path.
    (Bisaya: I-validate ug i-save ang hulagway sa asset; ibalik ang
    (relative_path, error) para isulod sa assets.image_path sa database.)

    Args:
        file_storage: Werkzeug FileStorage - the uploaded file.
        (Bisaya: ang na-upload nga file.)
        asset_code: the asset's own code (e.g. "AST00001"), used as the
            saved filename's prefix so it's identifiable on disk.
        (Bisaya: ang code sa asset - e.g. "AST00001" - gigamit isip prefix
        sa filename nga i-save, para makilala dayon sa disk kung kang kinsa
        ni nga hulagway.)

    Returns:
        tuple (relative_path, error) - exactly one is not None:
        - relative_path: str, e.g. "uploads/assets/AST00001-a1b2c3d4.jpg",
          ready to store in assets.image_path and serve via /static/.
        - error: str explaining why validation failed (see
          validate_image_upload above).
        (Bisaya: usa ka tuple (relative_path, error) - usa lang sa duha ang
        naay laman: ang `relative_path` andam na isulod sa database ug
        i-serve pinaagi sa /static/; ang `error` naa kung napakyas ang
        validation.)
    """
    error = validate_image_upload(file_storage)
    if error:
        return None, error

    os.makedirs(ASSET_IMAGE_DIR, exist_ok=True)

    ext = _extension(file_storage.filename)
    filename = f"{asset_code}-{secrets.token_hex(4)}.{ext}"
    relative_path = os.path.join(ASSET_IMAGE_SUBDIR, filename).replace(os.sep, "/")
    absolute_path = os.path.join(ASSET_IMAGE_DIR, filename)

    _save_resized_image(file_storage, absolute_path, ext, ASSET_IMAGE_MAX_DIMENSION)
    return relative_path, None


def save_staff_photo(file_storage, staff_username):
    """Validates and saves a staff profile photo; same contract as
    save_asset_image (see its docstring above for the (relative_path,
    error) return shape) - just keyed by staff_username instead of an
    asset code, and saved to its own subdir/dimension cap.
    (Bisaya: I-validate ug i-save ang profile photo sa staff; pareho ra sa
    save_asset_image sa taas - kini nga return shape (relative_path,
    error) - lahi lang ang gigamit nga key, mao ang staff_username, ug
    lahi nga subdir/gidak-on ang gigamit.)"""
    error = validate_image_upload(file_storage)
    if error:
        return None, error

    os.makedirs(STAFF_PHOTO_DIR, exist_ok=True)

    ext = _extension(file_storage.filename)
    filename = f"{staff_username}-{secrets.token_hex(4)}.{ext}"
    relative_path = os.path.join(STAFF_PHOTO_SUBDIR, filename).replace(os.sep, "/")
    absolute_path = os.path.join(STAFF_PHOTO_DIR, filename)

    _save_resized_image(file_storage, absolute_path, ext, STAFF_PHOTO_MAX_DIMENSION)
    return relative_path, None


def save_feedback_screenshot(file_storage, report_id):
    """Validates and saves one feedback screenshot; takes a single file, not
    a list - the caller loops over MAX_FEEDBACK_IMAGES files itself and
    calls this once per file. Same (relative_path, error) contract as
    save_asset_image above.
    (Bisaya: I-validate ug i-save ang usa ka feedback screenshot - usa ra
    ka file, dili listahan - ang nagtawag mismo ang mo-loop sa
    MAX_FEEDBACK_IMAGES ka file ug mutawag ani matag file. Pareho ra ka
    return shape (relative_path, error) sa save_asset_image sa taas.)"""
    error = validate_image_upload(file_storage)
    if error:
        return None, error

    os.makedirs(FEEDBACK_IMAGE_DIR, exist_ok=True)

    ext = _extension(file_storage.filename)
    filename = f"fb{report_id}-{secrets.token_hex(4)}.{ext}"
    relative_path = os.path.join(FEEDBACK_IMAGE_SUBDIR, filename).replace(os.sep, "/")
    absolute_path = os.path.join(FEEDBACK_IMAGE_DIR, filename)

    _save_resized_image(file_storage, absolute_path, ext, FEEDBACK_IMAGE_MAX_DIMENSION)
    return relative_path, None


def delete_feedback_screenshot(relative_path):
    """Deletes one previously-saved feedback screenshot from disk.
    (Bisaya: Tangtangon ang usa ka na-save nga feedback screenshot gikan sa
    disk.)"""
    _delete_static_file(relative_path, "feedback screenshot")


def _delete_static_file(relative_path, kind):
    """Best-effort delete; failures are logged, not raised, so a DB update isn't blocked.
    (Bisaya: Mo-try lang mo-tangtang - kung mapakyas, i-log ra kini, dili
    ni maka-hawa sa update sa database nga naghulat.)

    Args:
        relative_path: path relative to static/ (as stored in the DB
            column), or None/empty to no-op.
        (Bisaya: path nga relative sa static/ folder - sama sa gi-store sa
        database - o None/walay laman para walay buhaton.)
        kind: human-readable label for the log message on failure, e.g.
            "asset image".
        (Bisaya: mubo nga label para sa log message kung mapakyas, e.g.
        "asset image".)

    Returns:
        None.
        (Bisaya: walay gibalik.)
    """
    if not relative_path:
        return
    absolute_path = os.path.join(STATIC_DIR, relative_path)
    try:
        if os.path.exists(absolute_path):
            os.remove(absolute_path)
    except OSError as e:
        logger.warning(f"Failed to delete {kind} at {absolute_path}: {e}")


def delete_asset_image(relative_path):
    """Deletes one previously-saved asset image from disk.
    (Bisaya: Tangtangon ang usa ka na-save nga hulagway sa asset gikan sa
    disk.)"""
    _delete_static_file(relative_path, "asset image")


def delete_staff_photo(relative_path):
    """Deletes one previously-saved staff profile photo from disk.
    (Bisaya: Tangtangon ang usa ka na-save nga profile photo sa staff
    gikan sa disk.)"""
    _delete_static_file(relative_path, "staff photo")
