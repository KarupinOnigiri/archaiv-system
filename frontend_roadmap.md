# Frontend Roadmap — Toolroom App (Flask + htmx + Jinja2 + Tailwind/DaisyUI)

Approach: **HTML-first**. Routes render templates/partials instead of `jsonify()`.
JSON stays only where a non-HTML consumer genuinely needs it (see Phase 0).

---

## Phase 0 — Scaffolding (do this before touching any route)

1. **Install frontend tooling**
   - Tailwind CSS + DaisyUI (via npm, compiled to `static/css/output.css`)
   - htmx (single `<script>` include from CDN or vendored into `static/js/`)
2. **Create folder structure**
   ```
   templates/
     base.html                 # <html>, <head>, htmx + CSRF meta tag, sidebar/nav
     login.html
     setup.html                # temp-admin first-run screen
     dashboard.html
     assets/
       index.html               # full page
       _table.html              # partial: table body, swapped by htmx
       _row.html                # partial: single row, for inline edit/delete
       _form_modal.html         # partial: add/edit modal
     members/  staff/  transactions/  requests/  stats/
       (same pattern as assets/)
     partials/
       _pagination.html
       _toast.html               # success/error flash messages
   static/
     css/  js/
   ```
3. **Wire Jinja2 basics in `app.py`**
   - No new dependency — `render_template` is already part of Flask.
4. **CSRF for htmx**
   - Render the session's `csrf_token` into a `<meta name="csrf-token" content="...">` in `base.html`.
   - Add one global `htmx:configRequest` listener (in a small `static/js/app.js`) that reads that meta tag and attaches it as `X-CSRF-Token` on every htmx request. Write this once, never touch it again.
5. **Decide your JSON exceptions now, explicitly** (keep the list short):
   - `/setup/status` — fine as JSON, checked before any page renders
   - Anything a barcode scanner/external device hits directly
   - Everything else → HTML

Deliverable: a base layout that renders, htmx script loads, CSRF header fires correctly on a dummy `POST`.

---

## Phase 1 — Auth shell (small, but unblocks everything else)

1. `GET /login` → render `login.html` (plain form, not htmx — this is the one place a full page load/redirect is normal, since you're setting a cookie).
2. `GET /` (or a `before_request` check) → redirect to `/login` if no valid session, or to `/setup` if `setup_required`.
3. Wire `POST /api/v1/login` to still return JSON (fine here — it's driven by a plain form `fetch` or even a normal form POST + redirect), but **do** make sure a failed login re-renders `login.html` with an error rather than a raw JSON blob if you go the plain-form route.
4. Logout button → `POST /logout`, redirect to `/login`.

Deliverable: you can log in, land on an empty dashboard shell, log out.

---

## Phase 2 — One full vertical slice: Assets

Do this one resource **completely** before touching any other. This is where you settle every pattern you'll reuse everywhere else.

1. `GET /assets` → `assets/index.html` (page shell) which itself does an `hx-get` on load to `assets/_table.html` (or render the table server-side on first load, htmx only for subsequent filters/pagination — pick one and stay consistent).
2. Search/filter inputs → `hx-get` to the same table endpoint with `hx-trigger="keyup changed delay:300ms"`, targeting `#asset-table-body`.
3. Pagination → `hx-get` links/buttons, same target.
4. "Add Asset" button → `hx-get` a modal partial (`_form_modal.html`) into a `#modal` div.
5. Modal form submit → `hx-post` to `/assets/add`, target `#asset-table-body` (or `hx-swap="none"` + `HX-Trigger` response header to fire a `refreshTable` event — decide which convention you like and use it everywhere from here on).
6. Edit/Delete row actions → same modal-and-swap pattern.
7. Flash messages (success/error) → return an out-of-band swap (`hx-swap-oob`) targeting a toast container, driven by `log_action`'s existing success paths and your route's error branches.

Deliverable: Assets CRUD fully working end-to-end with htmx, styled with Tailwind/DaisyUI. This is your template for every other resource.

---

## Phase 3 — Replicate the pattern across remaining resources

Now that the conventions are fixed, this phase is mostly mechanical. Suggested order (roughly matches dependency order in your schema):

1. **Departments & Categories** — trivial, same table/modal pattern, good warm-up.
2. **Members** — list, add/edit, restrict/unrestrict, delete/reactivate, barcode lookup.
3. **Staff** — list, add/edit, password reset/unlock, delete/reactivate. Gate admin-only actions (like `request-deactivation`) behind role checks *in the template* (`{% if g.staff.role == 'admin' %}`) in addition to the existing server-side `@roles_required` — both layers matter.
4. **Transactions / Checkout flow** — this is your most interaction-heavy screen (scan member → build session → add items → checkout; separately, the return flow). Worth sketching the UI flow on paper before templating it.

---

## Phase 4 — Approval workflows

1. **Unrestriction requests** (`requests.py`) — queue view (admin/in_charge) + "my requests" view (tool_keeper), approve/deny/cancel actions.
2. **Admin deactivation requests** (`staff.py`) — same shape, slightly higher stakes UI (confirmation modal, show approval count / required_approvals progress).

These reuse every pattern from Phase 2 — just wire them up.

---

## Phase 5 — Dashboard & Stats

1. Build `dashboard.html` last, once you have real data flowing through the system to look at.
2. Each `stats.py` endpoint → either a small server-rendered table/list, or feed a lightweight chart (Chart.js is fine to include here — this is one legitimate place client-side JS earns its keep, since charts aren't htmx's job).

---

## Phase 6 — Polish & edge cases

- Empty states (no assets, no results after filter)
- Loading indicators on htmx requests (`hx-indicator`)
- Confirm dialogs on destructive actions (delete, deactivate) — `hx-confirm` covers the simple cases
- 401/403/404 from your existing error handlers — make sure `handle_http_exception` still returns JSON for `/api/v1/*` but you may want a separate HTML 404/403 page for direct navigation
- Session-expired mid-form — decide the UX (toast + redirect to login vs silent retry)

---

## Phase 7 — Before calling it done

- Walk every route in `assets.py`, `members.py`, `staff.py`, `transactions.py`, `requests.py` and confirm none still returns raw `jsonify()` by accident (except your Phase 0 exception list)
- Re-check the CSRF meta-tag listener fires on every `hx-post`/`hx-delete`, not just the ones you tested first
- Test the whole flow as each role (`admin`, `in_charge`, `tool_keeper`) — your RBAC is server-enforced already, but the UI should also hide actions a role can't take, so it doesn't dangle a button that 403s

---

**Rule of thumb throughout:** don't touch business logic (queries, validators, `log_action` calls) — only the last line of each route (`jsonify(...)` → `render_template(...)`) and whatever new template/partial it renders. If you find yourself rewriting query logic to make a template work, stop — that's a sign the route should stay as-is and you should adjust the template instead.
