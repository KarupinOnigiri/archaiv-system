"""Historical trend aggregation over rfid_events, for Monitoring's "Trends" tab -
kept separate from monitoring_service.py (which owns live/today-scoped state:
the alarm banner, the event table, record/deactivate) the same way
stats_service.py is split out from the day-to-day transaction/asset services.

No new schema: everything here reads the existing indexed detected_at/
classification/alarm_activated/alarm_deactivated_at columns. Two things this
data can't answer yet, documented rather than faked:
- Per-exit-gate/location breakdown - rfid_events has no gate/reader column,
  and the Pi-side listener (outside this repo) doesn't send one. Every event
  is treated as one monitored exit.
- True false-positive rate - there's no staff-facing "mark as false positive"
  flag on an event. `unknown_tag` rate is used as a documented proxy: a tag
  that matches no registered asset at all is the closest thing this schema
  has to "the alarm fired on nothing real," though a genuinely unauthorized
  removal (a real asset, wrong status) is excluded from that proxy on purpose.
"""

from services.monitoring_service import build_monitoring_filters


def _since_until_where(conn, args, extra_where=""):
    """Reuses build_monitoring_filters for since/until/classification/q parsing
    so trend queries accept exactly the same filter params as the live table,
    then layers on any trend-specific WHERE fragment (e.g. alarm_activated=1)."""
    where_clauses, params, _classification_filter, error = build_monitoring_filters(args)
    if error:
        return None, None, error
    if extra_where:
        where_clauses = list(where_clauses) + [extra_where]
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    return where_sql, params, None


def get_daily_event_volume(conn, args):
    """One row per (date, classification) in range - stacked-bar source."""
    where_sql, params, error = _since_until_where(conn, args)
    if error:
        return None, error
    rows = conn.execute(
        f"""
        SELECT date(rfid_events.detected_at) AS day, rfid_events.classification, COUNT(*) AS event_count
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN members ON members.id = rfid_events.member_id
        {where_sql}
        GROUP BY day, rfid_events.classification
        ORDER BY day
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows], None


def get_hourly_distribution(conn, args):
    """0-23 hour-of-day histogram, all classifications combined - local time
    (detected_at is already stored 'now','localtime'), so this reflects the
    tool room's actual clock, not UTC."""
    where_sql, params, error = _since_until_where(conn, args)
    if error:
        return None, error
    rows = conn.execute(
        f"""
        SELECT strftime('%H', rfid_events.detected_at) AS hour, COUNT(*) AS event_count
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN members ON members.id = rfid_events.member_id
        {where_sql}
        GROUP BY hour
        ORDER BY hour
        """,
        params,
    ).fetchall()
    by_hour = {r["hour"]: r["event_count"] for r in rows}
    return [{"hour": f"{h:02d}", "event_count": by_hour.get(f"{h:02d}", 0)} for h in range(24)], None


def get_alarm_response_times(conn, args):
    """Seconds between detection and the Master Key Card clearing it, for
    every alarm that HAS been deactivated in range (an alarm still active
    has no response time yet, so it's excluded rather than counted as 0
    or as still-pending). Returns the raw list plus avg/median/p90 -
    small N is expected here (alarms should be rare), so this stays a
    Python computation rather than a second SQL round trip."""
    where_sql, params, error = _since_until_where(
        conn, args, extra_where="rfid_events.alarm_activated = 1 AND rfid_events.alarm_deactivated_at IS NOT NULL"
    )
    if error:
        return None, error
    rows = conn.execute(
        f"""
        SELECT rfid_events.id, rfid_events.detected_at, rfid_events.alarm_deactivated_at,
               (julianday(rfid_events.alarm_deactivated_at) - julianday(rfid_events.detected_at)) * 86400.0 AS response_seconds
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN members ON members.id = rfid_events.member_id
        {where_sql}
        ORDER BY rfid_events.detected_at
        """,
        params,
    ).fetchall()

    seconds = sorted(r["response_seconds"] for r in rows if r["response_seconds"] is not None)
    n = len(seconds)
    if n == 0:
        stats = {"count": 0, "avg_seconds": None, "median_seconds": None, "p90_seconds": None}
    else:
        avg = sum(seconds) / n
        mid = n // 2
        median = seconds[mid] if n % 2 == 1 else (seconds[mid - 1] + seconds[mid]) / 2
        p90_index = min(n - 1, int(round(0.9 * (n - 1))))
        stats = {
            "count": n,
            "avg_seconds": round(avg, 1),
            "median_seconds": round(median, 1),
            "p90_seconds": round(seconds[p90_index], 1),
        }
    return {"events": [dict(r) for r in rows], **stats}, None


def get_unknown_tag_rate_trend(conn, args):
    """Daily unknown_tag count / total count - the documented false-positive
    proxy (see module docstring). Days with zero events are omitted rather
    than shown as a misleading 0% rate."""
    where_sql, params, error = _since_until_where(conn, args)
    if error:
        return None, error
    rows = conn.execute(
        f"""
        SELECT date(rfid_events.detected_at) AS day,
               COUNT(*) AS total_count,
               SUM(CASE WHEN rfid_events.classification = 'unknown_tag' THEN 1 ELSE 0 END) AS unknown_count
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN members ON members.id = rfid_events.member_id
        {where_sql}
        GROUP BY day
        ORDER BY day
        """,
        params,
    ).fetchall()
    return [
        {
            "day": r["day"],
            "total_count": r["total_count"],
            "unknown_count": r["unknown_count"],
            "unknown_rate": round(100 * r["unknown_count"] / r["total_count"], 1) if r["total_count"] else 0,
        }
        for r in rows
    ], None


def get_summary(conn, args):
    """Compact metric-list version of the Trends tab's KPIs, for Reports'
    "Monitoring Activity" widget - same since/until/classification/q filters
    (build_monitoring_filters), reduced to plain (label, value) rows since
    Reports' table widgets aren't chart-shaped. Reuses get_alarm_response_times
    for the response-time figures rather than re-deriving them."""
    where_sql, params, error = _since_until_where(conn, args)
    if error:
        return None, error
    rows = conn.execute(
        f"""
        SELECT rfid_events.classification, COUNT(*) AS event_count
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN members ON members.id = rfid_events.member_id
        {where_sql}
        GROUP BY rfid_events.classification
        """,
        params,
    ).fetchall()
    by_classification = {r["classification"]: r["event_count"] for r in rows}
    total_events = sum(by_classification.values())

    response, error = get_alarm_response_times(conn, args)
    if error:
        return None, error

    def fmt_seconds(s):
        if s is None:
            return "-"
        if s < 60:
            return f"{round(s)}s"
        return f"{round(s / 60)}m {round(s % 60)}s"

    metrics = [
        {"label": "Total Events", "value": total_events},
        {"label": "Authorized", "value": by_classification.get("authorized", 0)},
        {"label": "Unauthorized", "value": by_classification.get("unauthorized", 0)},
        {"label": "Unknown Tag", "value": by_classification.get("unknown_tag", 0)},
        {"label": "Alarms Cleared", "value": response["count"]},
        {"label": "Avg Alarm Response", "value": fmt_seconds(response["avg_seconds"])},
        {"label": "Median Alarm Response", "value": fmt_seconds(response["median_seconds"])},
    ]
    return metrics, None
