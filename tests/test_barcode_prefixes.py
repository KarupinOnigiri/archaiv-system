"""Barcode ID prefix settings (Asset/Member/Staff) - services/staff_service.py's
update_barcode_prefixes/_force_regenerate_prefixed_codes + routes/staff.py.
"""

from tests.conftest import TEST_PASSWORD, make_asset, make_member, make_staff


def update_prefixes(client, csrf, asset="AST", member="MBR", staff="STF", force_regenerate=False, password=TEST_PASSWORD):
    payload = {
        "asset_id_prefix": asset,
        "member_code_prefix": member,
        "staff_code_prefix": staff,
        "force_regenerate": force_regenerate,
    }
    if force_regenerate:
        payload["current_password"] = password
    return client.post(
        "/api/v1/barcode-prefixes/update",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )


# # # VALIDATION --------------------------------------------------------------


def test_accepts_common_symbols(as_admin, conn):
    client, csrf, _ = as_admin
    resp = update_prefixes(client, csrf, asset="A-1", member="M_2", staff="S.3")
    assert resp.status_code == 200, resp.get_json()
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'asset_id_prefix'").fetchone()
    assert row["value"] == "A-1"


def test_rejects_slash(as_admin):
    client, csrf, _ = as_admin
    resp = update_prefixes(client, csrf, asset="A/B")
    assert resp.status_code == 400


def test_rejects_space(as_admin):
    client, csrf, _ = as_admin
    resp = update_prefixes(client, csrf, asset="A B")
    assert resp.status_code == 400


def test_rejects_too_short(as_admin):
    client, csrf, _ = as_admin
    resp = update_prefixes(client, csrf, asset="A")
    assert resp.status_code == 400


def test_forbidden_for_in_charge(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = update_prefixes(client, csrf)
    assert resp.status_code == 403


# # # FORCE REGENERATE (password-gated) ---------------------------------------


def test_force_regenerate_requires_a_password_at_all(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/barcode-prefixes/update",
        json={"asset_id_prefix": "NEW", "member_code_prefix": "MBR", "staff_code_prefix": "STF", "force_regenerate": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_force_regenerate_requires_correct_password(as_admin):
    client, csrf, _ = as_admin
    resp = update_prefixes(client, csrf, asset="NEW", force_regenerate=True, password="wrong-password")
    assert resp.status_code == 401


def test_normal_save_does_not_touch_existing_codes(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    asset_id = make_asset(conn, category_id)
    old_code = conn.execute("SELECT asset_id FROM assets WHERE id = ?", (asset_id,)).fetchone()["asset_id"]

    resp = update_prefixes(client, csrf, asset="NEW")
    assert resp.status_code == 200

    code = conn.execute("SELECT asset_id FROM assets WHERE id = ?", (asset_id,)).fetchone()["asset_id"]
    assert code == old_code


def test_force_regenerate_rewrites_asset_code_and_matching_barcode(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    asset_id = make_asset(conn, category_id)  # barcode_id defaults to match asset_id

    resp = update_prefixes(client, csrf, asset="NEW", force_regenerate=True)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["regenerated"]["assets"] == 1

    row = conn.execute("SELECT asset_id, barcode_id FROM assets WHERE id = ?", (asset_id,)).fetchone()
    expected = f"NEW{asset_id:05d}"
    assert row["asset_id"] == expected
    assert row["barcode_id"] == expected  # carried along since it matched the old code


def test_force_regenerate_preserves_custom_barcode(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    asset_id = make_asset(conn, category_id, barcode_id="CUSTOM-LABEL-1")

    resp = update_prefixes(client, csrf, asset="NEW", force_regenerate=True)
    assert resp.status_code == 200, resp.get_json()

    row = conn.execute("SELECT asset_id, barcode_id FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["asset_id"] == f"NEW{asset_id:05d}"
    assert row["barcode_id"] == "CUSTOM-LABEL-1"  # untouched - never matched the generated code


def test_force_regenerate_covers_members_and_staff_too(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    member_id = make_member(conn, department_id)

    resp = update_prefixes(client, csrf, member="MEM", force_regenerate=True)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["regenerated"]["members"] >= 1
    assert "staff" in data["regenerated"]

    row = conn.execute("SELECT member_code FROM members WHERE id = ?", (member_id,)).fetchone()
    assert row["member_code"] == f"MEM{member_id:05d}"


def test_force_regenerate_logs_action(as_admin, conn, category_id):
    client, csrf, staff_id = as_admin
    make_asset(conn, category_id)
    update_prefixes(client, csrf, asset="NEW", force_regenerate=True)

    log = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'force_regenerated_prefixed_codes'"
    ).fetchone()
    assert log is not None
    assert log["staff_id"] == staff_id
