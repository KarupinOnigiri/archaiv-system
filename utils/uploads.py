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
    except (UnidentifiedImageError, OSError):
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
    'AST-00001') — used as a filename prefix purely for readability on
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


def delete_asset_image(relative_path):
    """Best-effort delete of a previously-saved asset image — e.g. when
    replacing one. Safe to call with None or a path that's already gone;
    failures are logged, not raised, since a missing file should never
    block a database update that's otherwise valid."""
    if not relative_path:
        return
    absolute_path = os.path.join(STATIC_DIR, relative_path)
    try:
        if os.path.exists(absolute_path):
            os.remove(absolute_path)
    except OSError as e:
        logger.warning(f"Failed to delete asset image at {absolute_path}: {e}")
