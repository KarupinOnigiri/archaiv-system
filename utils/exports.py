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
    the real extension.
    (Bisaya: Tangtangon ang extension - .csv, .xls, .xlsx - ug ang mga
    karakter nga dili safe gikan sa filename nga gi-type sa user, mo-fallback
    sa `fallback` kung wala nay mabilin nga magamit. Ang export format
    mismo - dili ang gi-type sa user - ang mo-desisyon sa tinuod nga
    extension, dili sila mapili ug lahi.)

    Args:
        raw: the filename text the user typed in the export form.
        (Bisaya: ang text nga gi-type sa user sa export form.)
        fallback: default name to use when `raw` is empty or becomes empty
            after stripping unsafe characters.
        (Bisaya: default nga ngalan gamiton kung walay laman ang `raw`, o
        nahimong walay laman human tangtangon ang mga unsafe nga karakter.)

    Returns:
        str - a filename safe to use on disk, with no extension.
        (Bisaya: usa ka filename nga safe gamiton sa disk, walay extension.)
    """
    raw = (raw or "").strip()
    if not raw:
        return fallback
    raw = _FILENAME_EXT_RE.sub("", raw).strip()
    raw = _FILENAME_SAFE_RE.sub("_", raw).strip("_")
    return raw or fallback


def default_export_dir():
    """<app root>/export - this is a self-hosted internal tool, not a
    shared public host, so exposing/using the app's own folder as the
    default save location is the intended behavior (matches
    routes/assets.py).
    (Bisaya: <app root>/export - kini nga app self-hosted ra, dili
    gi-share sa daghang tawo sa publiko, mao nga OK ra nga ang default
    save location mismong sulod sa folder sa app - tuyo gyud ni, dili
    sayop, pareho sa gigamit sa routes/assets.py.)

    Returns:
        str - absolute path to <app root>/export.
        (Bisaya: usa ka absolute path paingon sa <app root>/export.)
    """
    return os.path.join(current_app.root_path, "export")


def resolve_within_root(raw_path, default_dir):
    """Resolves raw_path to an absolute path guaranteed to stay inside
    the app root - a relative path is resolved against the app root; an
    absolute path is only honored if it's already inside the app root.
    Anything that would escape it falls back to default_dir instead.
    (Bisaya: I-resolve/husayon ang raw_path ngadto sa absolute path nga
    SIGURADO nga naa gihapon sulod sa app root - kung relative path,
    i-resolve base sa app root; kung absolute path, tugotan lang kung naa
    na diay ni sa sulod sa app root. Bisan unsa nga mo-gawas/mo-escape sa
    app root, mo-fallback ra sa default_dir - kini ang proteksyon batok sa
    "path traversal" nga pag-atake, e.g. gigamit "../../" para makagawas.)

    Args:
        raw_path: the user-typed path to resolve (may be relative,
            absolute, empty, or an attempted escape like "../../etc").
        (Bisaya: ang path nga gi-type sa user, husayon - pwede relative,
        absolute, walay laman, o pagsulay mo-gawas sa folder pareho sa
        "../../etc".)
        default_dir: fallback directory to use when raw_path is empty or
            would escape the app root.
        (Bisaya: fallback nga folder gamiton kung walay laman ang raw_path,
        o kung mo-gawas kini sa app root.)

    Returns:
        tuple (resolved_dir, was_clamped) - `resolved_dir` is the final
        absolute path to use; `was_clamped` is True when raw_path had to be
        rejected/replaced with default_dir because it tried to escape the
        app root.
        (Bisaya: usa ka tuple (resolved_dir, was_clamped) - ang
        `resolved_dir` mao ang absolute path nga gamiton; ang `was_clamped`
        True kung gi-reject/gi-ilisan ang raw_path sa default_dir tungod
        kay nisulay ni mo-gawas sa app root.)
    """
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
    """Resolves a user-entered save folder for an actual export. Falls
    back silently (no error surfaced) since this runs as a side effect of
    a file download.
    (Bisaya: I-resolve ang folder nga gi-type sa user para sa tinuod nga
    export. Kung dili valid, mo-fallback ra nga hilom - walay error nga
    ipakita - kay kini nagdagan isip side-effect lang sa pag-download sa
    file.)

    Args:
        raw_path: the user-typed save folder from the export form.
        (Bisaya: ang folder nga gi-type sa user sa export form.)

    Returns:
        tuple (resolved_dir, was_clamped) - see resolve_within_root() above.
        (Bisaya: usa ka tuple (resolved_dir, was_clamped) - tan-awa ang
        resolve_within_root() sa taas.)
    """
    return resolve_within_root(raw_path, default_export_dir())
