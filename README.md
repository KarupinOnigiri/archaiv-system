# Toolroom Inventory System

RFID/barcode-enabled tool tracking and access-control system built with
Flask + HTMX + SQLite, developed for the EIM tool room at RTC-KPVTC Davao.

For production deployment (Raspberry Pi), see [DEPLOYMENT.md](DEPLOYMENT.md).
For the application's security model, including its protections and
limitations, see [SECURITY.md](SECURITY.md).

## Local development setup

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env           # then fill in SECRET_KEY (see comments in the file)
python app.py
```

### CSS (Tailwind + DaisyUI)

`static/css/output.css` is a **generated build artifact** and must not be
edited manually. Source styling resides in `static/css/input.css`
(Tailwind import, DaisyUI theme tokens, custom `@layer` rules);
`output.css` is compiled from it by the Tailwind CLI.

`daisyui.mjs` and `daisyui-theme.mjs` (the DaisyUI plugin, vendored
rather than pulled from npm) are located at the **repository root**, not
under `static/`. This placement is required, not incidental: DaisyUI's
plugin resolves its own file location internally, and relocating it even
one directory deeper silently omits approximately 40% of the compiled
component CSS (`.drawer-side`, `.fab`, and others) with no build error to
indicate the failure - confirmed by rebuilding from both locations and
comparing the output. Any future relocation of these files must be
verified with a full rebuild and a diff against the committed
`output.css`.

The CLI binary itself is not checked into the repository (100MB+). It
must be downloaded once for local development:

1. Obtain the standalone `tailwindcss` executable for the target
   platform from the
   [Tailwind CSS releases page](https://github.com/tailwindlabs/tailwindcss/releases)
   (v4.x, to match `input.css`'s syntax).
2. Place it at `tools/tailwindcss.exe` (Windows) or `tools/tailwindcss`
   (macOS/Linux); this path is excluded from version control and remains
   local only.

Then, while editing styles:

```bash
./tools/tailwindcss.exe -i static/css/input.css -o static/css/output.css --watch
```

The regenerated `output.css` must be committed alongside the
corresponding `input.css` change, as the Pi deployment serves the
checked-in compiled file directly and does not run the Tailwind build
itself.

### JavaScript

- `static/js/app.js` - small, page-wide behavior (CSRF header wiring).
- `static/js/ui.js` - global UI chrome shared across every page (sidebar
  collapse state, the toast notification system). Both are loaded in
  `templates/base.html`.
- `static/js/*.min.js` - vendored third-party libraries; see
  `static/js/VENDORED.md`.
- All other behavior (modal-specific setup, form validation, htmx event
  wiring for a single component) resides inline within that component's
  own template partial, since it is only relevant while that partial is
  present on the page. This is the standard pattern for an htmx-driven
  application and is an intentional design choice rather than an
  oversight.

### Maintenance / one-off scripts

`tools/encrypt_existing_db.py` (retroactively encrypts an existing
plaintext database - see [SECURITY.md](SECURITY.md)) resides in `tools/`,
alongside the Tailwind CLI, as it is invoked manually and never imported
by the application.

`backup_db.py` remains at the repository root instead. Although it can
also be run standalone, `services/backup_service.py` imports it directly
(`import backup_db`) to perform the app's scheduled backups, making it a
runtime dependency rather than a standalone script.

`deploy/` holds the Pi provisioning script and systemd unit files - see
[DEPLOYMENT.md](DEPLOYMENT.md).

### `docs/` vs `export/`

`docs/` contains the project's documentation (architecture diagrams,
guides, etc.). It is committed to version control and maintained as part
of the source tree.

`export/` serves a different purpose: it is the application's default
output directory for user-initiated asset exports, defined by
`utils/exports.py`'s `default_export_dir()`. It is created automatically
at runtime and excluded from version control.
