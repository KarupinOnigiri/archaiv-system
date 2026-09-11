"""Historical trend aggregation over rfid_events, para sa Monitoring's
"Trends" tab - gilain sa monitoring_service.py (live/today-scoped state ra
kadto: alarm banner, event table, record/deactivate). Walay bag-ong
schema, gigamit ra ang naa nang indexed columns.

Duha ka butang dili pa masulbad ani nga data (documented, dili faked):
per-exit-gate/location breakdown (walay gate column ang rfid_events), ug
tinuod nga false-positive rate (gigamit na lang ang unknown_tag rate isip
proxy).
"""

from services.monitoring_service import build_monitoring_filters


def _since_until_where(conn, args, extra_where=""):
    """Gigamit pag-usab ang build_monitoring_filters para sa
    since/until/classification/q, dayon dugangan ug trend-specific nga
    WHERE fragment (e.g. alarm_activated=1).

    Returns: tuple (where_sql, params, error)."""
    where_clauses, params, _classification_filter, error = build_monitoring_filters(args)
    if error:
        return None, None, error
    if extra_where:
        where_clauses = list(where_clauses) + [extra_where]
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    return where_sql, params, None


def get_daily_event_volume(conn, args):
    """Usa ka row matag (petsa, classification) sulod sa gi-range -
    gigamit sa stacked-bar chart source.

    Returns: tuple (rows, error)."""
    where_sql, params, error = _since_until_where(conn, args)
    if error:
        return None, error
    rows = conn.execute(
        f"""
        SELECT date(rfid_events.detected_at) AS day, rfid_events.classification, COUNT(*) AS event_count
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN categories ON categories.id = assets.category_id
        LEFT JOIN members ON members.id = rfid_events.member_id
        {where_sql}
        GROUP BY day, rfid_events.classification
        ORDER BY day
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows], None


def get_hourly_distribution(conn, args):
    """Histogram sa oras 0-23 sa adlaw, gitapok ang tanang classification -
    localtime, dili UTC, mao gyud ang tinuod nga orasan sa tool room.

    Returns: tuple (list sa 24 ka entry - hour/event_count, error)."""
    where_sql, params, error = _since_until_where(conn, args)
    if error:
        return None, error
    rows = conn.execute(
        f"""
        SELECT strftime('%H', rfid_events.detected_at) AS hour, COUNT(*) AS event_count
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN categories ON categories.id = assets.category_id
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
    """Ihap sa segundo tali sa detection ug pag-tap sa Master Key Card nga
    nag-clear niini, para sa matag alarm nga NA-DEACTIVATE na - ang active
    pa gihapon nga alarm gilaktawan, dili i-ihap isip 0/pending. Python na
    lang ang mokwenta sa avg/median/p90 kay gamay ra ang N (rare ang alarm).

    Returns: tuple (dict {"events", avg/median/p90/count}, error)."""
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
        LEFT JOIN categories ON categories.id = assets.category_id
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


def get_rfid_response_timing(conn, args, row_limit=500):
    """Per-event millisecond timing (RFID acknowledgment response time;
    alarm activation response time) -
    distinct from get_alarm_response_times() above, which measures
    something else entirely (seconds-to-minutes until a human taps the
    Master Key Card to CLEAR an alarm, not milliseconds for the system to
    ACTIVATE it). See migration 0061 for where these three raw columns
    come from: client_detected_at_ms (the Pi's antenna, pi_controller/
    rfid_listener.py), server_processed_at_ms (this app, routes/
    monitoring.py's record endpoint), and alarm_relay_activated_at_ms (the
    Pi's follow-up call after gpio_relay.activate() actually fires).

    Both response times are computed here in Python (simple subtraction)
    rather than in SQL, and only for rows that actually have both of the
    relevant timestamps - an event from before this migration, or one
    where the Pi's follow-up call never landed (network hiccup - see
    api_client.report_alarm_relay_timing's docstring), just has a None for
    that one computed value rather than a misleading 0 or being dropped
    from the list entirely, since it may still have the OTHER timing value
    valid.

    Returns: tuple (list of per-event dicts, newest-first, capped at
    row_limit unless row_limit is None; error)."""
    where_sql, params, error = _since_until_where(conn, args)
    if error:
        return None, error
    # SQLite's LIMIT takes -1 to mean "no limit" - row_limit=None (the
    # export route's uncapped case) maps to that rather than omitting the
    # clause, so this stays one query either way instead of two branches.
    rows = conn.execute(
        f"""
        SELECT rfid_events.id, rfid_events.detected_at, rfid_events.rfid_tag_scanned,
               rfid_events.classification, rfid_events.alarm_activated,
               rfid_events.client_detected_at_ms, rfid_events.server_processed_at_ms,
               rfid_events.alarm_relay_activated_at_ms
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN categories ON categories.id = assets.category_id
        LEFT JOIN members ON members.id = rfid_events.member_id
        {where_sql}
        ORDER BY rfid_events.detected_at DESC
        LIMIT ?
        """,
        (*params, -1 if row_limit is None else row_limit),
    ).fetchall()

    events = []
    for r in rows:
        client_ms = r["client_detected_at_ms"]
        ack_ms = (
            r["server_processed_at_ms"] - client_ms
            if client_ms is not None and r["server_processed_at_ms"] is not None
            else None
        )
        alarm_ms = (
            r["alarm_relay_activated_at_ms"] - client_ms
            if client_ms is not None and r["alarm_relay_activated_at_ms"] is not None
            else None
        )
        events.append(
            {
                "id": r["id"],
                "detected_at": r["detected_at"],
                "rfid_tag_scanned": r["rfid_tag_scanned"],
                "classification": r["classification"],
                "alarm_activated": bool(r["alarm_activated"]),
                "ack_response_ms": ack_ms,
                "alarm_activation_ms": alarm_ms,
            }
        )

    def _stats(values):
        values = sorted(v for v in values if v is not None)
        n = len(values)
        if n == 0:
            return {"count": 0, "avg_ms": None, "median_ms": None, "min_ms": None, "max_ms": None}
        mid = n // 2
        median = values[mid] if n % 2 == 1 else (values[mid - 1] + values[mid]) / 2
        return {
            "count": n,
            "avg_ms": round(sum(values) / n, 1),
            "median_ms": round(median, 1),
            "min_ms": values[0],
            "max_ms": values[-1],
        }

    return {
        "events": events,
        "ack_response": _stats(e["ack_response_ms"] for e in events),
        "alarm_activation": _stats(e["alarm_activation_ms"] for e in events),
    }, None


def get_unknown_tag_rate_trend(conn, args):
    """Adlaw-adlaw nga unknown_tag count / total count - ang false-positive
    proxy (tan-awa ang module docstring). Adlaw nga walay event gilaktawan,
    dili ipakita isip sayop nga 0%.

    Returns: tuple (list sa {day, total_count, unknown_count,
    unknown_rate}, error)."""
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
        LEFT JOIN categories ON categories.id = assets.category_id
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
    """Compact nga bersyon sa Trends tab's KPIs, para sa Reports'
    "Monitoring Activity" widget - pareho ra ka filter, gipahamutang isip
    plain (label, value) rows kay dili chart-shaped ang table widget.
    Gigamit pag-usab ang get_alarm_response_times imbes kwentahon pag-usab.

    Returns: tuple (list sa {"label", "value"} dict, error)."""
    where_sql, params, error = _since_until_where(conn, args)
    if error:
        return None, error
    rows = conn.execute(
        f"""
        SELECT rfid_events.classification, COUNT(*) AS event_count
        FROM rfid_events
        LEFT JOIN assets ON assets.id = rfid_events.asset_id
        LEFT JOIN categories ON categories.id = assets.category_id
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
        """I-format ang segundo isip "12s" o "1m 5s" para ipakita; "-" kung None."""
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
