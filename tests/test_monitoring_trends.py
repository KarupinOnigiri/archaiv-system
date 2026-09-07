"""RFID monitoring "Trends" tab: historical aggregation over rfid_events -
routes/monitoring.py's /monitoring/trends + services/monitoring_stats_service.py.

Rows are inserted directly with explicit detected_at/alarm_deactivated_at
timestamps (rather than going through record_rfid_event, which always stamps
'now') so grouping-by-day/hour and response-time math can be tested precisely.
"""

from services import monitoring_stats_service
from tests.test_monitoring import enable_monitoring

EMPTY_ARGS = {}


def insert_event(conn, classification, detected_at, alarm_activated=0, alarm_deactivated_at=None):
    conn.execute(
        """
        INSERT INTO rfid_events (rfid_tag_scanned, classification, alarm_activated, detected_at, alarm_deactivated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (f"TAG-{detected_at}", classification, alarm_activated, detected_at, alarm_deactivated_at),
    )
    conn.commit()


def test_daily_event_volume_groups_by_day_and_classification(conn):
    insert_event(conn, "authorized", "2026-01-01 08:00:00")
    insert_event(conn, "authorized", "2026-01-01 09:00:00")
    insert_event(conn, "unknown_tag", "2026-01-01 10:00:00")
    insert_event(conn, "unauthorized", "2026-01-02 08:00:00")

    rows, error = monitoring_stats_service.get_daily_event_volume(conn, EMPTY_ARGS)
    assert error is None
    by_day = {}
    for r in rows:
        by_day.setdefault(r["day"], {})[r["classification"]] = r["event_count"]

    assert by_day["2026-01-01"]["authorized"] == 2
    assert by_day["2026-01-01"]["unknown_tag"] == 1
    assert by_day["2026-01-02"]["unauthorized"] == 1


def test_hourly_distribution_covers_all_24_hours(conn):
    insert_event(conn, "authorized", "2026-01-01 08:15:00")
    insert_event(conn, "authorized", "2026-01-01 08:45:00")

    rows, error = monitoring_stats_service.get_hourly_distribution(conn, EMPTY_ARGS)
    assert error is None
    assert len(rows) == 24
    by_hour = {r["hour"]: r["event_count"] for r in rows}
    assert by_hour["08"] == 2
    assert by_hour["00"] == 0


def test_alarm_response_times_only_counts_deactivated_alarms(conn):
    # Deactivated 30 seconds later.
    insert_event(conn, "unauthorized", "2026-01-01 08:00:00", alarm_activated=1, alarm_deactivated_at="2026-01-01 08:00:30")
    # Still active - excluded, not counted as 0.
    insert_event(conn, "unauthorized", "2026-01-01 09:00:00", alarm_activated=1, alarm_deactivated_at=None)
    # Not an alarm at all.
    insert_event(conn, "authorized", "2026-01-01 10:00:00")

    result, error = monitoring_stats_service.get_alarm_response_times(conn, EMPTY_ARGS)
    assert error is None
    assert result["count"] == 1
    assert result["avg_seconds"] == 30.0
    assert result["median_seconds"] == 30.0
    assert result["p90_seconds"] == 30.0


def test_alarm_response_times_empty_is_not_an_error(conn):
    result, error = monitoring_stats_service.get_alarm_response_times(conn, EMPTY_ARGS)
    assert error is None
    assert result["count"] == 0
    assert result["avg_seconds"] is None


def test_unknown_tag_rate_trend_computes_percentage(conn):
    insert_event(conn, "authorized", "2026-01-01 08:00:00")
    insert_event(conn, "authorized", "2026-01-01 09:00:00")
    insert_event(conn, "unknown_tag", "2026-01-01 10:00:00")

    rows, error = monitoring_stats_service.get_unknown_tag_rate_trend(conn, EMPTY_ARGS)
    assert error is None
    assert len(rows) == 1
    assert rows[0]["total_count"] == 3
    assert rows[0]["unknown_count"] == 1
    assert rows[0]["unknown_rate"] == 33.3


def test_since_until_filters_are_respected(conn):
    insert_event(conn, "authorized", "2026-01-01 08:00:00")
    insert_event(conn, "authorized", "2026-01-05 08:00:00")

    rows, error = monitoring_stats_service.get_daily_event_volume(conn, {"since": "2026-01-03"})
    assert error is None
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-01-05"


def test_invalid_since_returns_error_tuple(as_admin, conn):
    # build_monitoring_filters' error path calls jsonify(), which needs a
    # request context - go through the route (as every other caller does)
    # rather than the service function directly.
    enable_monitoring(conn)
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/monitoring/trends?since=not-a-date")
    assert resp.status_code == 400


# # # ROUTE -----------------------------------------------------------------


def test_trends_route_requires_monitoring_feature_enabled(as_admin, conn):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/monitoring/trends")
    assert resp.status_code == 403


def test_trends_route_returns_all_four_datasets(as_admin, conn):
    enable_monitoring(conn)
    insert_event(conn, "authorized", "2026-01-01 08:00:00")
    insert_event(conn, "unauthorized", "2026-01-01 09:00:00", alarm_activated=1, alarm_deactivated_at="2026-01-01 09:01:00")

    client, csrf, _ = as_admin
    resp = client.get("/api/v1/monitoring/trends")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "daily_volume" in data
    assert "hourly_distribution" in data
    assert "alarm_response" in data
    assert "unknown_tag_rate" in data
    assert data["alarm_response"]["count"] == 1


def test_trends_route_allowed_for_tool_keeper(as_tool_keeper, conn):
    """No role is excluded from Monitoring (see test_monitoring.py's module
    docstring) - this just needs the feature flag on."""
    enable_monitoring(conn)
    client, csrf, _ = as_tool_keeper
    resp = client.get("/api/v1/monitoring/trends")
    assert resp.status_code == 200


# # # SUMMARY (Reports' "Monitoring Activity" widget) ------------------------


def test_get_summary_counts_by_classification_and_response_times(conn):
    insert_event(conn, "authorized", "2026-01-01 08:00:00")
    insert_event(conn, "authorized", "2026-01-01 09:00:00")
    insert_event(conn, "unauthorized", "2026-01-01 10:00:00", alarm_activated=1, alarm_deactivated_at="2026-01-01 10:01:00")
    insert_event(conn, "unknown_tag", "2026-01-01 11:00:00")

    metrics, error = monitoring_stats_service.get_summary(conn, EMPTY_ARGS)
    assert error is None
    by_label = {m["label"]: m["value"] for m in metrics}
    assert by_label["Total Events"] == 4
    assert by_label["Authorized"] == 2
    assert by_label["Unauthorized"] == 1
    assert by_label["Unknown Tag"] == 1
    assert by_label["Alarms Cleared"] == 1
    assert by_label["Avg Alarm Response"] == "1m 0s"


def test_get_summary_zero_events(conn):
    metrics, error = monitoring_stats_service.get_summary(conn, EMPTY_ARGS)
    assert error is None
    by_label = {m["label"]: m["value"] for m in metrics}
    assert by_label["Total Events"] == 0
    assert by_label["Avg Alarm Response"] == "-"


def test_monitoring_summary_route_requires_monitoring_feature_enabled(as_admin, conn):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/monitoring-summary")
    assert resp.status_code == 403


def test_monitoring_summary_route_returns_metrics(as_admin, conn):
    enable_monitoring(conn)
    insert_event(conn, "authorized", "2026-01-01 08:00:00")

    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/monitoring-summary")
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert any(m["label"] == "Total Events" and m["value"] == 1 for m in results)


def test_monitoring_summary_route_forbidden_for_tool_keeper(as_tool_keeper, conn):
    enable_monitoring(conn)
    client, csrf, _ = as_tool_keeper
    resp = client.get("/api/v1/stats/monitoring-summary")
    assert resp.status_code == 403


def test_layout_hides_monitoring_activity_when_feature_disabled(as_admin, conn):
    """monitoring_activity is admin/in_charge-allowed by role, but should
    still be absent from the layout/widget list while the monitoring
    feature itself is off - routes/stats.py's _feature_disabled_widget_ids."""
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/layout")
    widget_ids = {w["id"] for w in resp.get_json()["widgets"]}
    assert "monitoring_activity" not in widget_ids

    enable_monitoring(conn)
    resp = client.get("/api/v1/stats/layout")
    widget_ids = {w["id"] for w in resp.get_json()["widgets"]}
    assert "monitoring_activity" in widget_ids


def test_export_includes_monitoring_activity_section(as_admin, conn):
    enable_monitoring(conn)
    insert_event(conn, "authorized", "2026-01-01 08:00:00")

    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/export?sections=monitoring_activity&format=csv")
    assert resp.status_code == 200
    assert b"Total Events" in resp.data
