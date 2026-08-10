import os
import secrets
import logging
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # matches the add-asset form's upload placeholder

# Served via static/uploads/assets/, same as any other static asset -- no separate route needed.
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
ASSET_IMAGE_SUBDIR = os.path.join("uploads", "assets")
ASSET_IMAGE_DIR = os.path.join(STATIC_DIR, ASSET_IMAGE_SUBDIR)

# Own subdir, separate from asset images -- different lifecycle (one per staff account)
# and avoids a staff_code/asset_id collision on the same filename prefix.
STAFF_PHOTO_SUBDIR = os.path.join("uploads", "staff")
STAFF_PHOTO_DIR = os.path.join(STATIC_DIR, STAFF_PHOTO_SUBDIR)

# Own subdir too -- zero-or-more per feedback report, never replaced in place.
FEEDBACK_IMAGE_SUBDIR = os.path.join("uploads", "feedback")
FEEDBACK_IMAGE_DIR = os.path.join(STATIC_DIR, FEEDBACK_IMAGE_SUBDIR)

# Cap per report on top of the per-file MAX_IMAGE_SIZE_BYTES cap -- more than this is abuse.
MAX_FEEDBACK_IMAGES = 5


def _extension(filename):
    if not filename or "." not in filename:
        return None
    return filename.rsplit(".", 1)[1].lower()


def validate_image_upload(file_storage):
    """Returns an error message string, or None if the upload is valid.
    Checks extension and size, then actually decodes the file with
    Pillow to confirm it's a real image — a renamed .exe with a .jpg
    extension and a spoofed Content-Type would pass the first two checks
    but fail this one."""
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
    # SyntaxError too: Pillow's PNG decoder raises that (not the other two) for a
    # valid signature with corrupt chunk data. Want a clean 400, not a 500 crash.
    except (UnidentifiedImageError, OSError, SyntaxError):
        return "File is not a valid image"
    finally:
        file_storage.stream.seek(0)

    return None


def save_asset_image(file_storage, asset_code):
    """Validates and saves an asset image. Returns (relative_path, error) -- relative_path
    goes straight into assets.image_path and is None on failure. asset_code (e.g. 'AST00001')
    is just a filename prefix for readability; a random suffix keeps re-uploads from colliding."""
    error = validate_image_upload(file_storage)
    if error:
        return None, error

    os.makedirs(ASSET_IMAGE_DIR, exist_ok=True)

    ext = _extension(file_storage.filename)
    filename = f"{asset_code}-{secrets.token_hex(4)}.{ext}"
    relative_path = os.path.join(ASSET_IMAGE_SUBDIR, filename).replace(os.sep, "/")
    absolute_path = os.path.join(ASSET_IMAGE_DIR, filename)

    file_storage.save(absolute_path)
    return relative_path, None


def save_staff_photo(file_storage, staff_username):
    """Validates and saves a staff profile photo. Same contract as save_asset_image
    -- returns (relative_path, error) for staff.photo_path, random suffix appended."""
    error = validate_image_upload(file_storage)
    if error:
        return None, error

    os.makedirs(STAFF_PHOTO_DIR, exist_ok=True)

    ext = _extension(file_storage.filename)
    filename = f"{staff_username}-{secrets.token_hex(4)}.{ext}"
    relative_path = os.path.join(STAFF_PHOTO_SUBDIR, filename).replace(os.sep, "/")
    absolute_path = os.path.join(STAFF_PHOTO_DIR, filename)

    file_storage.save(absolute_path)
    return relative_path, None


def save_feedback_screenshot(file_storage, report_id):
    """Validates and saves one feedback screenshot. Same (relative_path, error) contract.
    Takes a single file rather than a list -- the route calls this per file and cleans up
    any already-saved files itself if one in the batch fails."""
    error = validate_image_upload(file_storage)
    if error:
        return None, error

    os.makedirs(FEEDBACK_IMAGE_DIR, exist_ok=True)

    ext = _extension(file_storage.filename)
    filename = f"fb{report_id}-{secrets.token_hex(4)}.{ext}"
    relative_path = os.path.join(FEEDBACK_IMAGE_SUBDIR, filename).replace(os.sep, "/")
    absolute_path = os.path.join(FEEDBACK_IMAGE_DIR, filename)

    file_storage.save(absolute_path)
    return relative_path, None


def delete_feedback_screenshot(relative_path):
    _delete_static_file(relative_path, "feedback screenshot")


def _delete_static_file(relative_path, kind):
    """Best-effort delete, e.g. when replacing an image. Safe with None/missing paths;
    failures are logged, not raised, since a missing file shouldn't block a valid DB update."""
    if not relative_path:
        return
    absolute_path = os.path.join(STATIC_DIR, relative_path)
    try:
        if os.path.exists(absolute_path):
            os.remove(absolute_path)
    except OSError as e:
        logger.warning(f"Failed to delete {kind} at {absolute_path}: {e}")


def delete_asset_image(relative_path):
    _delete_static_file(relative_path, "asset image")


def delete_staff_photo(relative_path):
    _delete_static_file(relative_path, "staff photo")
