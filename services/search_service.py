"""Query-building para sa routes/search.py's global search bar (Ctrl/Cmd+K).

Dili full-text index - LIKE-based matching ra, pareho sa gigamit sa
matag entity's own local search - reused diri para pareho ra ang resulta
sa direktang search sa Inventory/Members/Staff's own box, capped ug
gi-tapok sa usa ka dropdown. Skip dayon ang category kung disabled ang
feature flag niini, o kung dili tugotan sa role sa staff.
"""

from utils.formatters import full_name
from utils.validators import escape_like_wildcards

MIN_QUERY_LENGTH = 2
MAX_RESULTS_PER_CATEGORY = 5

# Static "jump to page" results - same convention as base.html's own sidebar
# nav_items loop (see its role/feature_flags gating), just re-expressed as a
# plain visibility check here since this has no request/template context to
# reuse that Jinja loop directly. Kept in the same order the sidebar shows
# them in. No DB query needed, so these are always searched first regardless
# of query length elsewhere in the app - but still gated behind
# MIN_QUERY_LENGTH like everything else, so a 1-character query doesn't spam
# half the nav as "results".
_PAGES = [
    {"label": "Dashboard", "href": "/", "visible": lambda staff, flags: True},
    {"label": "Inventory", "href": "/assets", "visible": lambda staff, flags: flags.get("assets")},
    {"label": "Members", "href": "/members", "visible": lambda staff, flags: flags.get("members")},
    {"label": "Borrow & Return", "href": "/transactions", "visible": lambda staff, flags: flags.get("transactions")},
    {"label": "Approvals", "href": "/requests", "visible": lambda staff, flags: flags.get("requests")},
    {"label": "Notifications", "href": "/notifications", "visible": lambda staff, flags: True},
    {"label": "Reports", "href": "/reports", "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge", "tool_keeper") and flags.get("reports")},
    {"label": "Staff", "href": "/staff", "visible": lambda staff, flags: staff["role"] == "admin"},
    {"label": "Monitoring", "href": "/monitoring", "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge", "tool_keeper") and flags.get("monitoring")},
    {"label": "Activity Log", "href": "/audit-log", "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge", "tool_keeper")},
    {"label": "Feedback", "href": "/feedback", "visible": lambda staff, flags: staff["role"] == "admin"},
    {"label": "System", "href": "/system", "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge")},
    # Not a sidebar tab (System is its own only entry point - see system.html's
    # own comment on this same card), but it's still a real page, not a modal,
    # so it belongs here rather than in _SETTINGS below.
    {"label": "Barcode Labels", "href": "/barcodes", "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge")},
]


def _search_pages(query, staff, feature_flags):
    """Susihon ang _PAGES list para sa mga label nga naay match sa query,
    filtered gyud sa ilang own "visible" lambda (role/feature-flag check)
    para dili makita ang page nga bawal tan-awon sa niining staff.

    Args:
        query: ang search text nga gi-type sa staff.
        staff: staff row - gigamit sa role check.
        feature_flags: dict {name: bool} - gigamit sa flag check.

    Returns:
        list[dict] - matching pages ra, {"label", "href"} matag entry.
    """
    q = query.lower()
    return [
        {"label": page["label"], "href": page["href"]}
        for page in _PAGES
        if q in page["label"].lower() and page["visible"](staff, feature_flags)
    ]


# Static "do a thing" shortcuts - same modals/endpoints and same role/feature-
# flag gating as the Dashboard's own Quick Actions card (home.html), just
# reachable from anywhere via search instead of only from the Dashboard.
# "checkout" is the one irregular entry: it needs the same hx-vals (a fresh
# client_id) and hx-trigger guard (Checkout.countOk(), ui.js) home.html's own
# button uses, plus the dedicated #checkout-modal-container target (base.html
# - a global, page-independent docking area for open checkout sessions) -
# every other action is a plain hx-get into #modal-container.
_QUICK_ACTIONS = [
    {
        "label": "Checkout",
        "hx_get": "/api/v1/checkout-sessions/start-form",
        "hx_target": "#checkout-modal-container",
        "hx_swap": "beforeend",
        "checkout_guard": True,
        "visible": lambda staff, flags: flags.get("transactions"),
    },
    {
        "label": "Return Items",
        "hx_get": "/api/v1/transactions/return-scan-form",
        "hx_target": "#modal-container",
        "hx_swap": "innerHTML",
        "checkout_guard": False,
        "visible": lambda staff, flags: flags.get("transactions"),
    },
    {
        "label": "Add Member",
        "hx_get": "/api/v1/members/add-form",
        "hx_target": "#modal-container",
        "hx_swap": "innerHTML",
        "checkout_guard": False,
        "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge") and flags.get("members"),
    },
    {
        "label": "Add Asset",
        "hx_get": "/api/v1/assets/add-form",
        "hx_target": "#modal-container",
        "hx_swap": "innerHTML",
        "checkout_guard": False,
        "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge") and flags.get("assets"),
    },
]


def _search_quick_actions(query, staff, feature_flags):
    """Same pattern as _search_pages pero para sa _QUICK_ACTIONS list -
    ang "visible" key ra ang gi-strip out sa result kay internal detail ra
    na, dili kinahanglan sa template.

    Args: query, staff, feature_flags - same shape as _search_pages.
    Returns: list[dict] - matching quick actions, walay "visible" key.
    """
    q = query.lower()
    return [
        {k: v for k, v in action.items() if k != "visible"}
        for action in _QUICK_ACTIONS
        if q in action["label"].lower() and action["visible"](staff, feature_flags)
    ]


# System page's own settings_card()s (templates/system.html) - same
# title/hx_get/hx_target pairs, same role gating as each card's own section
# there (Security & Access and Data & Maintenance are admin-only; Lending
# Policy/Notifications/Hardware go to admin and in_charge alike) - kept in
# sync by hand since system.html builds these from live settings values
# (current limits, thresholds, etc.) this static list has no reason to
# duplicate; only the destination (title + endpoint) needs to match.
# hx_target defaults to '#modal-container' like the macro itself; Notification
# Settings is the one exception (system.html passes '#global-modal-container').
_SETTINGS = [
    {"label": "Session Settings", "hx_get": "/api/v1/session-settings/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] == "admin"},
    {"label": "Account Security", "hx_get": "/api/v1/account-security/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] == "admin"},
    {"label": "Loan Limits", "hx_get": "/api/v1/loan-limits/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge")},
    {"label": "Loan Duration", "hx_get": "/api/v1/loan-duration/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge")},
    {"label": "Checkout Session Timeout", "hx_get": "/api/v1/checkout-session-settings/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge")},
    {"label": "Damage Restriction", "hx_get": "/api/v1/damage-restriction/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge")},
    {"label": "Notification Settings", "hx_get": "/api/v1/notification-settings/manage-form", "hx_target": "#global-modal-container",
     "visible": lambda staff, flags: staff["role"] in ("admin", "in_charge")},
    {"label": "Feature Access", "hx_get": "/api/v1/feature-flags/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] == "admin"},
    {"label": "Alarm Settings", "hx_get": "/api/v1/alarm-settings/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] == "admin"},
    {"label": "Barcode ID Prefixes", "hx_get": "/api/v1/barcode-prefixes/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] == "admin"},
    {"label": "Database Backups", "hx_get": "/api/v1/backup-settings/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] == "admin"},
    {"label": "Auto-Delete Archived Items", "hx_get": "/api/v1/archive-purge-settings/manage-form", "hx_target": "#modal-container",
     "visible": lambda staff, flags: staff["role"] == "admin"},
]


def _search_settings(query, staff, feature_flags):
    """Same pattern gihapon, para sa _SETTINGS list - System page's mga
    settings card, role-gated sunod sa ilang own "visible" check.

    Args: query, staff, feature_flags.
    Returns: list[dict] - matching settings entries.
    """
    q = query.lower()
    return [
        {"label": s["label"], "hx_get": s["hx_get"], "hx_target": s["hx_target"], "hx_swap": "innerHTML"}
        for s in _SETTINGS
        if q in s["label"].lower() and s["visible"](staff, feature_flags)
    ]


# Account dropdown's own modals/actions (base.html's sidebar-account popup) -
# each already-existing <dialog> is just showModal()'d directly (they're
# always present in the DOM on every page, unlike an htmx-fetched form), so
# these need no hx-get at all - see _global_search_results.html's rendering
# for this category. Theme options call setThemeMode() (ui.js) the exact same
# way the account menu's own theme submenu buttons do.
_ACCOUNT_ACTIONS = [
    {"label": "My Account", "onclick_js": "document.getElementById('my-account-modal').showModal()"},
    {"label": "Change Password", "onclick_js": "document.getElementById('change-password-modal').showModal()"},
    {"label": "Change RFID PIN", "onclick_js": "document.getElementById('change-rfid-pin-modal').showModal()"},
    {"label": "Settings & Privacy", "onclick_js": "document.getElementById('settings-privacy-modal').showModal()"},
    {"label": "Display & Accessibility", "onclick_js": "document.getElementById('display-accessibility-modal').showModal()"},
    {"label": "Help", "onclick_js": "document.getElementById('help-support-modal').showModal()"},
    {"label": "Help & User Manual", "onclick_js": "document.getElementById('full-help-modal').showModal()"},
    {"label": "Keyboard Shortcuts", "onclick_js": "document.getElementById('keyboard-shortcuts-modal').showModal()"},
    {"label": "Give Feedback", "onclick_js": "document.getElementById('give-feedback-modal').showModal()"},
    {"label": "Light Mode", "onclick_js": "setThemeMode('light')"},
    {"label": "Dark Mode", "onclick_js": "setThemeMode('dark')"},
    {"label": "Mixed Mode", "onclick_js": "setThemeMode('mixed')"},
    {"label": "Log Out", "onclick_js": "document.getElementById('logout-confirm-modal').showModal()"},
]


def _search_account_actions(query):
    """Susihon ang _ACCOUNT_ACTIONS list - walay role/feature gate diri kay
    naa naman gyud tanan na dialog sa DOM sa matag page, walay hx-get pa
    gikinahanglan.

    Args: query - ang search text.
    Returns: list[dict] - matching account actions {"label", "onclick_js"}.
    """
    q = query.lower()
    return [action for action in _ACCOUNT_ACTIONS if q in action["label"].lower()]


def _search_assets(conn, like):
    """Mag-search sa non-deleted assets base sa name/barcode_id/asset_id/
    rfid_id, LIKE-based match, capped sa MAX_RESULTS_PER_CATEGORY.

    Args:
        conn: open sqlite3.Connection.
        like: pre-wrapped "%query%" nga string, ready na para sa LIKE.

    Returns:
        list[dict] - matching assets (id, name, asset_id, barcode_id,
        status).
    """
    rows = conn.execute(
        """
        SELECT id, name, asset_id, barcode_id, status
        FROM assets
        WHERE deleted_at IS NULL
          AND (name LIKE ? ESCAPE '\\' OR barcode_id LIKE ? ESCAPE '\\' OR asset_id LIKE ? ESCAPE '\\' OR rfid_id LIKE ? ESCAPE '\\')
        ORDER BY name
        LIMIT ?
        """,
        (like, like, like, like, MAX_RESULTS_PER_CATEGORY),
    ).fetchall()
    return [dict(row) for row in rows]


def _search_members(conn, like):
    """Mag-search sa active members base sa first_name/last_name/
    barcode_id/member_code, LIKE-based, capped sa MAX_RESULTS_PER_CATEGORY.
    Ang full_name gina-compute per row para sayon na i-display sa template.

    Args: conn, like ("%query%" nga string).
    Returns: list[dict] - matching members, naay dugang "full_name" key.
    """
    rows = conn.execute(
        """
        SELECT id, first_name, last_name, middle_name, suffix, barcode_id, member_code, is_restricted
        FROM members
        WHERE is_active = 1
          AND (first_name LIKE ? ESCAPE '\\' OR last_name LIKE ? ESCAPE '\\' OR barcode_id LIKE ? ESCAPE '\\' OR member_code LIKE ? ESCAPE '\\')
        ORDER BY first_name
        LIMIT ?
        """,
        (like, like, like, like, MAX_RESULTS_PER_CATEGORY),
    ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["full_name"] = full_name(row["first_name"], row["last_name"], row["middle_name"], row["suffix"])
        results.append(item)
    return results


def _search_staff(conn, like):
    """Mag-search sa active staff base sa username/staff_code/first_name/
    last_name, LIKE-based, capped sa MAX_RESULTS_PER_CATEGORY - admin ra
    ang caller ani per global_search()'s own role check sa ubos.

    Args: conn, like ("%query%" nga string).
    Returns: list[dict] - matching staff, naay dugang "full_name" key.
    """
    rows = conn.execute(
        """
        SELECT staff.id, staff.username, staff.staff_code, staff.role,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.is_active = 1
          AND (staff.username LIKE ? ESCAPE '\\' OR staff.staff_code LIKE ? ESCAPE '\\'
               OR members.first_name LIKE ? ESCAPE '\\' OR members.last_name LIKE ? ESCAPE '\\')
        ORDER BY members.first_name
        LIMIT ?
        """,
        (like, like, like, like, MAX_RESULTS_PER_CATEGORY),
    ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["full_name"] = full_name(row["first_name"], row["last_name"], row["middle_name"], row["suffix"])
        results.append(item)
    return results


def global_search(conn, query, staff, feature_flags):
    """Mobalik ug dict sa tanang category (quick_actions, settings,
    account_actions, pages, assets, members, staff_results). Kung short
    ra kaayo ang query (below MIN_QUERY_LENGTH), tanan walay laman - ang
    dropdown magpabilin nga sirado imbes mopakita "no results", kay dili
    man gyud kini tinuod nga search pa."""
    query = (query or "").strip()
    # "staff_results", not "staff" - base.html's global context processor
    # already injects "staff" everywhere (the CURRENTLY LOGGED IN user), and
    # this dict gets unpacked as **result straight into render_template;
    # naming this list "staff" would silently shadow that for the one
    # template this renders.
    result = {
        "query": query, "quick_actions": [], "settings": [], "account_actions": [],
        "pages": [], "assets": [], "members": [], "staff_results": [],
    }
    if len(query) < MIN_QUERY_LENGTH:
        return result

    like = f"%{escape_like_wildcards(query)}%"

    result["quick_actions"] = _search_quick_actions(query, staff, feature_flags)
    result["settings"] = _search_settings(query, staff, feature_flags)
    result["account_actions"] = _search_account_actions(query)
    result["pages"] = _search_pages(query, staff, feature_flags)
    if feature_flags.get("assets"):
        result["assets"] = _search_assets(conn, like)
    if feature_flags.get("members"):
        result["members"] = _search_members(conn, like)
    if staff["role"] == "admin":
        result["staff_results"] = _search_staff(conn, like)

    return result
