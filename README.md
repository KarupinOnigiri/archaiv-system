# Toolroom Inventory System

RFID/barcode-enabled tool tracking and access-control system built with
Flask + HTMX + SQLite, developed for the EIM tool room at RTC-KPVTC Davao.

For production deployment (Raspberry Pi), see [DEPLOYMENT.md](DEPLOYMENT.md).
For the app's security model and what it does/doesn't protect against, see
[SECURITY.md](SECURITY.md).

## Local development setup

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env           # then fill in SECRET_KEY (see comments in the file)
python app.py
```

### CSS (Tailwind + DaisyUI)

`static/css/output.css` is a **generated build artifact** — never edit it
by hand. Source styling lives in `static/css/input.css` (Tailwind import,
DaisyUI theme tokens, custom `@layer` rules); `output.css` is compiled from
it by the Tailwind CLI.

`daisyui.mjs` and `daisyui-theme.mjs` (the DaisyUI plugin, vendored rather
than pulled from npm) live at the **repository root**, not under `static/`.
That placement is load-bearing, not accidental clutter: DaisyUI's plugin
resolves its own file location internally, and relocating it even one
directory deeper silently drops ~40% of the compiled component CSS
(`.drawer-side`, `.fab`, and others go missing) with no build error to
flag it — confirmed by rebuilding from both locations and diffing the
output. Verify with a full rebuild + diff against the committed
`output.css` before ever moving these files again.

The CLI binary itself isn't checked into the repo (100MB+). Download it
once for local development:

1. Grab the standalone `tailwindcss` executable for your platform from the
   [Tailwind CSS releases page](https://github.com/tailwindlabs/tailwindcss/releases)
   (v4.x, to match `input.css`'s syntax).
2. Place it at `tools/tailwindcss.exe` (Windows) or `tools/tailwindcss`
   (macOS/Linux) — this path is gitignored, so it stays local-only.

Then, while editing styles:

```bash
./tools/tailwindcss.exe -i static/css/input.css -o static/css/output.css --watch
```

Commit the regenerated `output.css` alongside your `input.css` change —
the Pi deployment serves the checked-in compiled file directly and does
not run the Tailwind build itself.

### JavaScript

- `static/js/app.js` — small, page-wide behavior (CSRF header wiring).
- `static/js/ui.js` — global UI chrome shared across every page (sidebar
  collapse state, the toast notification system). Both load in
  `templates/base.html`.
- `static/js/*.min.js` — vendored third-party libraries; see
  `static/js/VENDORED.md`.
- Everything else (modal-specific setup, form validation, htmx event
  wiring for a single component) lives inline in that component's own
  template partial, since it only matters while that partial is on the
  page — this is the normal pattern for an htmx-driven app and is
  intentional, not something to "clean up" into a shared file.

### Maintenance / one-off scripts

`tools/encrypt_existing_db.py` (retroactively encrypts an existing plaintext
database — see [SECURITY.md](SECURITY.md)) lives in `tools/`, alongside the
Tailwind CLI, since it's a script you run by hand and never import.

`backup_db.py` stays at the repository root instead, even though you can
also run it standalone — `services/backup_service.py` imports it directly
(`import backup_db`) for the app's own scheduled backups, so it's a real
runtime dependency, not just a script.

`deploy/` holds the Pi provisioning script and systemd unit files — see
[DEPLOYMENT.md](DEPLOYMENT.md).

### `docs/` vs `export/`

`docs/` holds project documentation (architecture diagrams, etc.) —
committed, not generated. `export/` is different: it's the app's own
default save location for user-initiated asset exports
(`utils/exports.py`'s `default_export_dir()`), recreated automatically at
runtime, and gitignored — nothing that lands there should be committed.
