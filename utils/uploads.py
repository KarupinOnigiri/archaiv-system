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
    """Downscales and re-saves the upload; falls back to a plain save on any Pillow error."""
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
    if not filename or "." not in filename:
        return None
    return filename.rsplit(".", 1)[1].lower()


def validate_image_upload(file_storage):
    """Checks extension, size, and actually decodes the file to confirm it's a real image."""
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
    """Validates and saves an asset image; returns (relative_path, error) for assets.image_path."""
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
    """Validates and saves a staff profile photo; same contract as save_asset_image."""
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
    """Validates and saves one feedback screenshot; takes a single file, not a list."""
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
    _delete_static_file(relative_path, "feedback screenshot")


def _delete_static_file(relative_path, kind):
    """Best-effort delete; failures are logged, not raised, so a DB update isn't blocked."""
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
