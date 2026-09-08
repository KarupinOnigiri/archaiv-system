import os
import re
from flask import current_app

# Clamps a user-typed save folder to stay inside the app root.

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]+")
_FILENAME_EXT_RE = re.compile(r"\.(csv|xlsx?)$", re.IGNORECASE)


def sanitize_export_filename(raw, fallback):
    """Tangtangon ang extension ug ang mga unsafe nga karakter sa filename
    nga gi-type sa user, mo-fallback sa `fallback` kung wala nay mabilin.
    Ang export format (dili ang gi-type sa user) ang mo-desisyon sa
    tinuod nga extension.

    Returns: str - filename nga safe gamiton, walay extension."""
    raw = (raw or "").strip()
    if not raw:
        return fallback
    raw = _FILENAME_EXT_RE.sub("", raw).strip()
    raw = _FILENAME_SAFE_RE.sub("_", raw).strip("_")
    return raw or fallback


def default_export_dir():
    """<app root>/export - self-hosted internal tool ra ni, dili shared
    public host, mao nga OK ra nga app's own folder ang default save
    location.

    Returns: str - absolute path paingon sa <app root>/export."""
    return os.path.join(current_app.root_path, "export")


def resolve_within_root(raw_path, default_dir):
    """I-resolve ang raw_path ngadto sa absolute path nga SIGURADO nga naa
    gihapon sulod sa app root - proteksyon batok sa "path traversal" nga
    pag-atake (e.g. "../../etc"). Mo-escape man ni, mo-fallback sa
    default_dir.

    Returns: tuple (resolved_dir, was_clamped) - was_clamped True kung
    gi-reject ang raw_path kay nisulay mo-gawas sa app root."""
    app_root = os.path.realpath(current_app.root_path)

    raw_path = (raw_path or "").strip()
    if not raw_path:
        return default_dir, False

    candidate = raw_path if os.path.isabs(raw_path) else os.path.join(app_root, raw_path)
    resolved = os.path.realpath(candidate)

    try:
        inside_root = os.path.commonpath([resolved, app_root]) == app_root
    except ValueError:
        # e.g. different drives on Windows - definitely not inside app_root
        inside_root = False

    if not inside_root:
        return default_dir, True
    return resolved, False


def resolve_export_dir(raw_path):
    """I-resolve ang save folder nga gi-type sa user para sa export. Kung
    dili valid, mo-fallback ra nga hilom, walay error nga ipakita.

    Returns: tuple (resolved_dir, was_clamped) - see resolve_within_root()."""
    return resolve_within_root(raw_path, default_export_dir())
