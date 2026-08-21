import os
import re
from flask import current_app

# Clamps a user-typed save folder to stay inside the app root.

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]+")
_FILENAME_EXT_RE = re.compile(r"\.(csv|xlsx?)$", re.IGNORECASE)


def sanitize_export_filename(raw, fallback):
    """Strips any extension and unsafe characters from a user-supplied
    export filename, falling back to `fallback` if nothing usable is
    left. The export format (not whatever the user typed) always decides
    the real extension."""
    raw = (raw or "").strip()
    if not raw:
        return fallback
    raw = _FILENAME_EXT_RE.sub("", raw).strip()
    raw = _FILENAME_SAFE_RE.sub("_", raw).strip("_")
    return raw or fallback


def default_export_dir():
    """<app root>/export -- this is a self-hosted internal tool, not a
    shared public host, so exposing/using the app's own folder as the
    default save location is the intended behavior (matches
    routes/assets.py)."""
    return os.path.join(current_app.root_path, "export")


def resolve_within_root(raw_path, default_dir):
    """Resolves raw_path to an absolute path guaranteed to stay inside
    the app root -- a relative path is resolved against the app root; an
    absolute path is only honored if it's already inside the app root.
    Anything that would escape it falls back to default_dir instead.
    Returns (resolved_dir, was_clamped)."""
    app_root = os.path.realpath(current_app.root_path)

    raw_path = (raw_path or "").strip()
    if not raw_path:
        return default_dir, False

    candidate = raw_path if os.path.isabs(raw_path) else os.path.join(app_root, raw_path)
    resolved = os.path.realpath(candidate)

    try:
        inside_root = os.path.commonpath([resolved, app_root]) == app_root
    except ValueError:
        # e.g. different drives on Windows -- definitely not inside app_root
        inside_root = False

    if not inside_root:
        return default_dir, True
    return resolved, False


def resolve_export_dir(raw_path):
    """Resolves a user-entered save folder for an actual export. Falls
    back silently (no error surfaced) since this runs as a side effect of
    a file download."""
    return resolve_within_root(raw_path, default_export_dir())
