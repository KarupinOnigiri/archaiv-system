import os
import secrets
import logging
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5MB — matches the limit already
# shown in the add-asset form's upload placeholder.

# Images live under static/uploads/assets/, alongside the app's other
# static assets (e.g. static/images/bglogin.jpg) — same serving
# mechanism (url_for('static', filename=...)), no separate route needed.
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
ASSET_IMAGE_SUBDIR = os.path.join("uploads", "assets")
ASSET_IMAGE_DIR = os.path.join(STATIC_DIR, ASSET_IMAGE_SUBDIR)

# Staff profile photos get their own subdir alongside asset images, rather
# than sharing ASSET_IMAGE_DIR — different lifecycle/ownership (one per
# staff account vs. one-or-more per asset), and keeping them apart means a
# staff_code and an asset_id can never collide on the same filename prefix.
STAFF_PHOTO_SUBDIR = os.path.join("uploads", "staff")
STAFF_PHOTO_DIR = os.path.join(STATIC_DIR, STAFF_PHOTO_SUBDIR)

# Feedback screenshots -- own subdir for the same reason staff photos got
# one: different lifecycle/ownership (zero-or-more per feedback report,
# never replaced in place -- see save_feedback_screenshot below) than
# asset images or staff photos.
FEEDBACK_IMAGE_SUBDIR = os.path.join("uploads", "feedback")
FEEDBACK_IMAGE_DIR = os.path.join(STATIC_DIR, FEEDBACK_IMAGE_SUBDIR)

# A staff member could plausibly want to attach a handful of screenshots
# to one report (e.g. a multi-step bug); more than this is almost
# certainly a mistake or abuse, and each one is already capped at
# MAX_IMAGE_SIZE_BYTES on top of this.
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
    # SyntaxError alongside the two you'd expect -- Pillow's PNG decoder
    # raises it (not UnidentifiedImageError/OSError) for a file with a
    # valid PNG signature but corrupt chunk data, e.g. a bad CRC. Caught
    # here now rather than a latent gap: this validator previously only
    # saw single-file, staff-controlled uploads (one asset photo, one
    # profile photo), but feedback screenshots (routes/feedback.py) let
    # any staff member submit up to five at once, raising the odds of a
    # truncated/corrupt file reaching this function -- that should come
    # back as an ordinary 400, not crash the request with a 500.
    except (UnidentifiedImageError, OSError, SyntaxError):
        return "File is not a valid image"
    finally:
        file_storage.stream.seek(0)

    return None


def save_asset_image(file_storage, asset_code):
    """Validates and saves an uploaded image for an asset.

    Returns (relative_path, error). relative_path is what gets stored in
    assets.image_path — relative to /static, so templates can render it
    with url_for('static', filename=asset.image_path). error is None on
    success, and relative_path is None on failure.

    asset_code should be the asset's human-readable code (e.g.
    'AST00001') — used as a filename prefix purely for readability on
    disk; a random suffix is still appended so re-uploads never collide
    with or overwrite a cached copy of the old image."""
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
    """Validates and saves an uploaded staff profile photo.

    Same contract as save_asset_image: returns (relative_path, error),
    relative_path is what gets stored in staff.photo_path — relative to
    /static, so templates render it with
    url_for('static', filename=staff.photo_path). error is None on
    success, and relative_path is None on failure.

    staff_username is used as a filename prefix purely for readability on
    disk (same reasoning as asset_code in save_asset_image); a random
    suffix is still appended so a re-upload never collides with or
    overwrites a cached copy of the old photo."""
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
    """Validates and saves one screenshot attached to a feedback report.

    Same contract as save_asset_image/save_staff_photo: returns
    (relative_path, error). Called once per file by the route for a
    multi-file upload (see routes/feedback.py) rather than taking a list
    itself, so one bad file among several doesn't need special-case
    partial-rollback handling -- the route just stops at the first error
    and nothing committed so far is orphaned (it deletes any files already
    saved in that same request)."""
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
    """Shared best-effort delete for both asset images and staff photos --
    e.g. when replacing one. Safe to call with None or a path that's
    already gone; failures are logged, not raised, since a missing file
    should never block a database update that's otherwise valid."""
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
