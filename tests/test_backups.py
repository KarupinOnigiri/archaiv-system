"""Database backup settings/manual-run/download/delete - routes/staff.py's
backup routes + services/staff_service.py + services/backup_service.py.

The actual backup_database() call (backup_db.py) uses SQLite's online
backup API against a REAL, fixed on-disk directory
(backup_db.DEFAULT_BACKUP_DIR = <project root>/backups) - it is NOT
test-isolated by tests/conftest.py's fresh_db fixture (that only
monkeypatches database.DB_PATH/INSTANCE_DIR). Actually running it here
would write real files into the real project directory. Every test below
either monkeypatches backup_db.DEFAULT_BACKUP_DIR to a pytest tmp_path
before touching the filesystem, or stubs out run_backup_now entirely so
backup_database() never executes at all - never call the real
trigger_manual_backup/backup_database path in a test.
"""

import os

import backup_db
import services.staff_service as staff_service
from tests.conftest import TEST_PASSWORD


def make_backup_file(backup_dir, name="toolroom_20260101_120000.db", content=b"fake-db-bytes"):
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, name)
    with open(path, "wb") as f:
        f.write(content)
    return path


# # # SETTINGS (admin only) -------------------------------------------------


def test_manage_form_forbidden_for_non_admin(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = client.get("/api/v1/backup-settings/manage-form")
    assert resp.status_code == 403


def test_manage_form_success(as_admin, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/backup-settings/manage-form")
    assert resp.status_code == 200


def test_update_settings_success(as_admin, conn):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/backup-settings/update",
        json={"enabled": True, "interval_hours": 12},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'auto_backup_interval_hours'"
    ).fetchone()
    assert row["value"] == "12"


def test_update_settings_rejects_out_of_range_interval(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/backup-settings/update",
        json={"enabled": True, "interval_hours": 200},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_update_settings_rejects_non_integer_interval(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/backup-settings/update",
        json={"enabled": True, "interval_hours": "not-a-number"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_update_settings_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/backup-settings/update",
        json={"enabled": True, "interval_hours": 6},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


# # # MANUAL RUN - run_backup_now stubbed, backup_database() never executes --


def test_run_now_success(as_admin, conn, monkeypatch):
    monkeypatch.setattr(staff_service, "run_backup_now", lambda: True)
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/backup-settings/run-now", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "Backup completed"


def test_run_now_records_last_backup_at(as_admin, conn, monkeypatch):
    """Keeps the real trigger_manual_backup -> run_backup_now -> backup_db.
    backup_database() wiring intact, but swaps backup_database() itself for
    a no-op so nothing actually touches disk - proves _set_last_backup_at
    still runs off that real chain, not just off a fully-stubbed route."""
    from services import backup_service

    monkeypatch.setattr(backup_db, "backup_database", lambda *a, **k: None)
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/backup-settings/run-now", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert backup_service.get_last_backup_at(conn) is not None


def test_run_now_reports_failure(as_admin, monkeypatch):
    monkeypatch.setattr(staff_service, "run_backup_now", lambda: False)
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/backup-settings/run-now", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 500


def test_run_now_forbidden_for_in_charge(as_in_charge, monkeypatch):
    monkeypatch.setattr(staff_service, "run_backup_now", lambda: True)
    client, csrf, _ = as_in_charge
    resp = client.post("/api/v1/backup-settings/run-now", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


# # # LIST / DOWNLOAD / DELETE (backup_db.DEFAULT_BACKUP_DIR monkeypatched) ----


def test_list_backups_empty_before_any_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path / "nonexistent"))
    assert staff_service.list_backups() == []


def test_list_backups_ignores_non_matching_filenames(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    make_backup_file(tmp_path, "toolroom_20260101_120000.db")
    (tmp_path / "not-a-backup.txt").write_text("junk")
    names = [b["name"] for b in staff_service.list_backups()]
    assert names == ["toolroom_20260101_120000.db"]


def test_list_backups_newest_first(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    make_backup_file(tmp_path, "toolroom_20260101_120000.db")
    make_backup_file(tmp_path, "toolroom_20260201_120000.db")
    names = [b["name"] for b in staff_service.list_backups()]
    assert names == ["toolroom_20260201_120000.db", "toolroom_20260101_120000.db"]


def test_download_backup_success(as_admin, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    make_backup_file(tmp_path, "toolroom_20260101_120000.db")
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/backup-settings/download/toolroom_20260101_120000.db")
    assert resp.status_code == 200
    assert resp.data == b"fake-db-bytes"


def test_download_backup_not_found(as_admin, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/backup-settings/download/toolroom_20260101_120000.db")
    assert resp.status_code == 404


def test_download_backup_rejects_path_traversal(as_admin, monkeypatch, tmp_path):
    """resolve_backup_path's filename regex + realpath containment check --
    Flask's own URL routing already collapses "../" segments before this
    ever runs, so this proves the *regex* itself would reject a malicious
    filename shape too, not just rely on Flask's routing behavior."""
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    path, error_msg, status = staff_service.resolve_backup_path("../../etc/passwd")
    assert path is None
    assert status == 400


def test_download_backup_forbidden_for_tool_keeper(as_tool_keeper, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    make_backup_file(tmp_path, "toolroom_20260101_120000.db")
    client, csrf, _ = as_tool_keeper
    resp = client.get("/api/v1/backup-settings/download/toolroom_20260101_120000.db")
    assert resp.status_code == 403


def test_delete_confirm_not_found(as_admin, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/backup-settings/delete-confirm/toolroom_20260101_120000.db")
    assert resp.status_code == 404


def test_delete_requires_password(as_admin, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    make_backup_file(tmp_path, "toolroom_20260101_120000.db")
    client, csrf, _ = as_admin
    resp = client.delete(
        "/api/v1/backup-settings/delete/toolroom_20260101_120000.db", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_delete_rejects_wrong_password(as_admin, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    make_backup_file(tmp_path, "toolroom_20260101_120000.db")
    client, csrf, _ = as_admin
    resp = client.delete(
        "/api/v1/backup-settings/delete/toolroom_20260101_120000.db",
        json={"current_password": "wrong"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 401


def test_delete_success(as_admin, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    path = make_backup_file(tmp_path, "toolroom_20260101_120000.db")
    client, csrf, _ = as_admin
    resp = client.delete(
        "/api/v1/backup-settings/delete/toolroom_20260101_120000.db",
        json={"current_password": TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert not os.path.exists(path)


def test_delete_not_found(as_admin, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_db, "DEFAULT_BACKUP_DIR", str(tmp_path))
    client, csrf, _ = as_admin
    resp = client.delete(
        "/api/v1/backup-settings/delete/toolroom_20260101_120000.db",
        json={"current_password": TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 404
