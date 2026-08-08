-- 0032_add_report_layouts.sql
-- Backs the new customizable/draggable Reports page: each staff member can
-- reorder and show/hide report widgets to their own taste, and that choice
-- has to persist across sessions/devices the same way any other per-user
-- preference in this app does (notification prefs, session settings), not
-- just live in page-local JS state that resets on reload. One row per
-- staff account (not per staff+widget) -- the whole layout is written and
-- read as a single JSON blob via services/report_layout_service.py, same
-- "one JSON column" shape used for e.g. staff notification prefs being
-- individual columns would have meant a migration every time a widget is
-- added/removed from the registry in routes/stats.py; JSON keeps the
-- widget catalog free to evolve without a schema change each time.
CREATE TABLE report_layouts (
  staff_id INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
  layout_json TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
);
