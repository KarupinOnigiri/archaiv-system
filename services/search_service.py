"""Query-building for routes/search.py's global search bar (content header, Ctrl/Cmd+K).

Not a full-text index - just the same LIKE-based matching each entity's own local
search already does (services/asset_service.py's get_assets_table_context,
services/member_service.py's get_members_table_context, services/staff_service.py's
get_staff_table_context), reused here so results feel consistent with what typing
the same query into Inventory/Members/Staff's own search box would find, just
capped and merged into one dropdown. Each category is skipped outright rather than
returned empty when its own feature flag is off, or (staff) when the caller's role
can't see the Staff tab at all - same visibility rules those tabs themselves enforce.
"""

from utils.formatters import full_name

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
    {"label": "Modern Dark Mode", "onclick_js": "setThemeMode('modern')"},
    {"label": "Log Out", "onclick_js": "document.getElementById('logout-confirm-modal').showModal()"},
]


def _search_account_actions(query):
    q = query.lower()
    return [action for action in _ACCOUNT_ACTIONS if q in action["label"].lower()]


def _search_assets(conn, like):
    rows = conn.execute(
        """
        SELECT id, name, asset_id, barcode_id, status
        FROM assets
        WHERE deleted_at IS NULL
          AND (name LIKE ? OR barcode_id LIKE ? OR asset_id LIKE ? OR rfid_id LIKE ?)
        ORDER BY name
        LIMIT ?
        """,
        (like, like, like, like, MAX_RESULTS_PER_CATEGORY),
    ).fetchall()
    return [dict(row) for row in rows]


def _search_members(conn, like):
    rows = conn.execute(
        """
        SELECT id, first_name, last_name, middle_name, suffix, barcode_id, member_code, is_restricted
        FROM members
        WHERE is_active = 1
          AND (first_name LIKE ? OR last_name LIKE ? OR barcode_id LIKE ? OR member_code LIKE ?)
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
    rows = conn.execute(
        """
        SELECT staff.id, staff.username, staff.staff_code, staff.role,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.is_active = 1
          AND (staff.username LIKE ? OR staff.staff_code LIKE ?
               OR members.first_name LIKE ? OR members.last_name LIKE ?)
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
    """Returns {"query": str, "quick_actions": [...], "settings": [...], "account_actions": [...],
    "pages": [...], "assets": [...], "members": [...], "staff_results": [...]} - an empty
    query (below MIN_QUERY_LENGTH) returns every list empty, which the template renders as
    nothing at all (dropdown stays closed) rather than a "no results" message, since that's
    not really a search yet."""
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

    like = f"%{query}%"

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
