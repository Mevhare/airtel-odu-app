"""SQLite storage for history and events.

One file, no schema migrations -- every table is created if missing. Samples are
written by the collector thread and read by the HTTP handler, so the connection
is opened per call rather than shared.
"""

import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wifi.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_samples (
    ts INTEGER PRIMARY KEY, rsrp REAL, rsrq REAL, sinr REAL,
    band TEXT, cell_id INTEGER, pci INTEGER, earfcn INTEGER,
    network_type TEXT, carriers INTEGER, connected INTEGER
);
CREATE TABLE IF NOT EXISTS usage_samples (
    ts INTEGER PRIMARY KEY,
    session_down INTEGER, session_up INTEGER,
    month_down INTEGER, month_up INTEGER,
    total_down INTEGER, total_up INTEGER,
    down_speed INTEGER, up_speed INTEGER
);
CREATE TABLE IF NOT EXISTS device_samples (
    ts INTEGER, mac TEXT, hostname TEXT, ip TEXT, band TEXT, rssi INTEGER,
    down_bytes INTEGER, up_bytes INTEGER, down_speed INTEGER, up_speed INTEGER,
    PRIMARY KEY (ts, mac)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS cycles (
    start_ts INTEGER PRIMARY KEY, baseline INTEGER, baseline_ts INTEGER
);

-- Our own accounting. Every raw counter reading is turned into a delta and
-- added to the hour it landed in, so the ledger is ours: it survives the
-- devices resetting their counters, and it can be sliced by any period.
-- source is 'wan' for the outdoor unit, or 'dev:<mac>' for one client.
CREATE TABLE IF NOT EXISTS traffic (
    hour INTEGER NOT NULL,
    source TEXT NOT NULL,
    down INTEGER NOT NULL DEFAULT 0,
    up INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, source)
);
-- The previous reading of each counter, so the next delta can be worked out.
CREATE TABLE IF NOT EXISTS counters (
    source TEXT PRIMARY KEY, down INTEGER, up INTEGER, ts INTEGER
);

-- Airtel texts a usage total for the previous day every morning. That is the
-- operator's own billing figure and it arrives whether or not this dashboard
-- was running, so it fills in the days we did not watch. Kept here because the
-- modem only holds about 40 messages before they roll off.
CREATE TABLE IF NOT EXISTS carrier_usage (
    day TEXT PRIMARY KEY, bytes INTEGER, msisdn TEXT, ts INTEGER
);

-- Odds and ends that must outlive a restart but are not measurements: which
-- login derivation the outdoor unit accepted, and anything similar later.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_mac_ts ON device_samples (mac, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_traffic_hour ON traffic (hour);
"""

# A counter that goes backwards means the device restarted and began again from
# zero, so the new reading is itself the traffic since. A counter that jumps by
# an absurd amount in one poll is a glitch, not a gigabyte -- 10 GB in a single
# interval is far past what this link can carry, so it is dropped.
MAX_DELTA = 10 * 1024 ** 3
# The same idea, but as a rate: no real link here sustains much more than
# this, so a delta implying a higher rate is a glitch regardless of how long
# the gap was. Scaling the cap by elapsed time (see _delta) is what lets a
# reading taken after the dashboard was off for hours be credited in full,
# instead of getting silently capped the way a genuine glitch would be.
MAX_RATE = 125 * 1024 ** 2  # ~1 Gbps, far past anything this link carries


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        # WAL keeps the collector's writes from blocking dashboard reads.
        conn.execute("PRAGMA journal_mode=WAL")


def record_signal(row):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO signal_samples "
            "(ts, rsrp, rsrq, sinr, band, cell_id, pci, earfcn, network_type, carriers, connected) "
            "VALUES (:ts, :rsrp, :rsrq, :sinr, :band, :cell_id, :pci, :earfcn, "
            ":network_type, :carriers, :connected)",
            row,
        )


def record_usage(row):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO usage_samples "
            "(ts, session_down, session_up, month_down, month_up, total_down, total_up, "
            "down_speed, up_speed) "
            "VALUES (:ts, :session_down, :session_up, :month_down, :month_up, "
            ":total_down, :total_up, :down_speed, :up_speed)",
            row,
        )


def record_devices(ts, devices):
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO device_samples "
            "(ts, mac, hostname, ip, band, rssi, down_bytes, up_bytes, down_speed, up_speed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(ts, d["mac"], d["hostname"], d["ip"], d["band"], d["rssi"],
              d["down_bytes"], d["up_bytes"], d["down_speed"], d["up_speed"])
             for d in devices],
        )


def get_setting(key, default=None):
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


def log_event(kind, detail=""):
    with connect() as conn:
        conn.execute("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
                     (int(time.time()), kind, detail))


def recent_events(limit=100):
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def history(table, hours=24, columns="*"):
    since = int(time.time()) - hours * 3600
    with connect() as conn:
        rows = conn.execute(
            "SELECT %s FROM %s WHERE ts >= ? ORDER BY ts" % (columns, table), (since,)
        ).fetchall()
    return [dict(r) for r in rows]


def device_history(mac, hours=24):
    since = int(time.time()) - hours * 3600
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts, down_bytes, up_bytes, rssi FROM device_samples "
            "WHERE mac = ? AND ts >= ? ORDER BY ts", (mac, since)
        ).fetchall()
    return [dict(r) for r in rows]


def uptime_ratio(hours=24):
    """Fraction of samples in the window that saw a live connection."""
    since = int(time.time()) - hours * 3600
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, SUM(connected) AS up "
            "FROM signal_samples WHERE ts >= ?", (since,)
        ).fetchone()
    if not row or not row["total"]:
        return None
    return (row["up"] or 0) / row["total"]


# -- our own traffic ledger --------------------------------------------------


def accumulate(readings, ts=None):
    """Fold raw counter readings into hourly buckets.

    ``readings`` maps a source name to ``(down, up)`` as the device currently
    reports them. Only the *change* since the previous reading is stored, so a
    device rebooting and starting from zero costs us nothing, and the ledger
    stays correct across firmware counter quirks we do not control -- and,
    since the previous reading is whatever this same ledger last saw rather
    than anything tied to the current process, a gap where the dashboard
    itself was off is bridged the same way: the next reading's delta just
    covers the whole gap, spread across the hours it took (see _spread).

    A counter that goes backwards -- the outdoor unit rebooting, or a client
    re-associating with the router -- means whatever it reads now is traffic
    since that restart, so it is credited rather than lost. One that only
    dips slightly is firmware jitter, not a restart, and is ignored either way.
    """
    ts = int(ts or time.time())

    with connect() as conn:
        previous = {row["source"]: row for row in
                    conn.execute("SELECT source, down, up, ts FROM counters")}

        deltas = []
        for source, (down, up) in readings.items():
            down, up = int(down or 0), int(up or 0)
            was = previous.get(source)
            if was is None:
                # First sight of this counter: establish a baseline only. Its
                # current value is history we did not watch happen.
                pass
            else:
                elapsed = max(0, ts - was["ts"])
                change = (_delta(down, was["down"], elapsed),
                          _delta(up, was["up"], elapsed))
                if change != (0, 0):
                    deltas.extend(
                        (hour, source, part_down, part_up)
                        for hour, part_down, part_up
                        in _spread(was["ts"], ts, change[0], change[1]))
            conn.execute(
                "INSERT INTO counters (source, down, up, ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET down = ?, up = ?, ts = ?",
                (source, down, up, ts, down, up, ts))

        if deltas:
            conn.executemany(
                "INSERT INTO traffic (hour, source, down, up) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(hour, source) DO UPDATE SET "
                "down = down + excluded.down, up = up + excluded.up",
                deltas)


def _spread(start, end, down, up):
    """Attribute one delta to the hours it actually spans.

    A reading is everything since the previous reading. Normally that is a few
    seconds and belongs to one hour. After the dashboard has been off for a
    while it can cover many hours, and dropping the whole lot into the hour
    sampling resumed would draw a spike on the chart that never happened.
    Nobody can know how the traffic was really distributed across the gap, so
    it is spread evenly: wrong in the detail, right in the total, and much
    closer to the truth than a single false peak.
    """
    start, end = int(start or 0), int(end)
    hour_of = lambda value: value - value % 3600   # noqa: E731
    if start <= 0 or end <= start or hour_of(start) == hour_of(end):
        return [(hour_of(end), down, up)]

    span = end - start
    pieces = []
    hour = hour_of(start)
    while hour < end:
        covered = min(end, hour + 3600) - max(start, hour)
        pieces.append((hour, covered / span))
        hour += 3600

    out = []
    spent_down = spent_up = 0
    for index, (hour, share) in enumerate(pieces):
        if index == len(pieces) - 1:            # the remainder, so nothing is lost
            part_down, part_up = down - spent_down, up - spent_up
        else:
            part_down, part_up = int(down * share), int(up * share)
            spent_down += part_down
            spent_up += part_up
        if part_down or part_up:
            out.append((hour, part_down, part_up))
    return out


def _delta(now, before, elapsed):
    # The longer the gap since the last reading, the more traffic it can
    # honestly contain -- a flat cap would treat a real day's usage after the
    # dashboard was off as a glitch and throw the whole thing away.
    ceiling = max(MAX_DELTA, int(elapsed * MAX_RATE))
    if now >= before:
        change = now - before
        return change if change <= ceiling else 0
    # A real restart lands the counter near zero, and the new value is genuinely
    # traffic since. A counter that merely twitches backwards is firmware jitter;
    # crediting that as fresh traffic on a lifetime counter reading 8 TB would
    # invent gigabytes out of nothing, so it is ignored.
    return now if now < before // 2 and now <= ceiling else 0


def traffic_rows(since, until=None, source=None):
    """Hourly buckets in a window. ``source`` of None means the WAN total."""
    until = int(until or time.time())
    query = "SELECT hour, SUM(down) AS down, SUM(up) AS up FROM traffic " \
            "WHERE hour >= ? AND hour <= ? "
    args = [int(since), until]
    if source == "*devices":
        query += "AND source LIKE 'dev:%' "
    elif source:
        query += "AND source = ? "
        args.append(source)
    else:
        query += "AND source = 'wan' "
    query += "GROUP BY hour ORDER BY hour"
    with connect() as conn:
        return [dict(r) for r in conn.execute(query, args).fetchall()]


def traffic_by_device(since, until=None):
    until = int(until or time.time())
    with connect() as conn:
        rows = conn.execute(
            "SELECT source, SUM(down) AS down, SUM(up) AS up FROM traffic "
            "WHERE hour >= ? AND hour <= ? AND source LIKE 'dev:%' "
            "GROUP BY source ORDER BY SUM(down) + SUM(up) DESC", (int(since), until)
        ).fetchall()
    return [{"mac": r["source"][4:], "down": r["down"], "up": r["up"]} for r in rows]


def traffic_total(since, until=None, source=None):
    rows = traffic_rows(since, until, source)
    return sum(r["down"] + r["up"] for r in rows)


def tracked_since():
    """When our own ledger starts. Anything earlier we simply did not see."""
    with connect() as conn:
        row = conn.execute("SELECT MIN(hour) AS first FROM traffic").fetchone()
    return row["first"] if row and row["first"] else None


# -- what the operator says --------------------------------------------------


def record_carrier_usage(reports):
    """Store the daily totals parsed out of Airtel's SMS. Idempotent."""
    if not reports:
        return
    now = int(time.time())
    with connect() as conn:
        conn.executemany(
            "INSERT INTO carrier_usage (day, bytes, msisdn, ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET bytes = excluded.bytes, "
            "msisdn = excluded.msisdn",
            [(r["date"], r["bytes"], r.get("msisdn"), now) for r in reports])


def carrier_tracked_since():
    """When Airtel's own daily texts start. Independent of our measured ledger."""
    with connect() as conn:
        row = conn.execute("SELECT MIN(day) AS first FROM carrier_usage").fetchone()
    return _day_start(row["first"]) if row and row["first"] else None


def carrier_days(since=None, until=None):
    """Daily operator totals, oldest first, as {day, ts, bytes}."""
    query = "SELECT day, bytes, msisdn FROM carrier_usage"
    args = []
    if since is not None:
        query += " WHERE day >= ?"
        args.append(_day_string(since))
        if until is not None:
            query += " AND day <= ?"
            args.append(_day_string(until))
    query += " ORDER BY day"
    with connect() as conn:
        rows = conn.execute(query, args).fetchall()
    return [{"day": r["day"], "ts": _day_start(r["day"]), "bytes": r["bytes"],
             "msisdn": r["msisdn"]} for r in rows]


def carrier_msisdn():
    with connect() as conn:
        row = conn.execute("SELECT msisdn FROM carrier_usage "
                           "WHERE msisdn IS NOT NULL AND msisdn != '' "
                           "ORDER BY day DESC LIMIT 1").fetchone()
    return row["msisdn"] if row else None


def _day_string(ts):
    import datetime
    return datetime.date.fromtimestamp(ts).isoformat()


def _day_start(day):
    import datetime
    date = datetime.date.fromisoformat(day)
    return int(datetime.datetime.combine(date, datetime.time.min).timestamp())


def cycle_bounds(billing_day, today=None):
    """Start and end of the billing cycle containing ``today``."""
    import datetime

    today = today or datetime.date.today()
    day = min(billing_day, 28)  # keep the anchor valid in every month
    start_date = today.replace(day=day)
    if today.day < day:
        start_date = (start_date.replace(day=1) - datetime.timedelta(days=1)).replace(day=day)
    if start_date.month == 12:
        end_date = start_date.replace(year=start_date.year + 1, month=1)
    else:
        end_date = start_date.replace(month=start_date.month + 1)

    midnight = datetime.time.min
    return (int(datetime.datetime.combine(start_date, midnight).timestamp()),
            int(datetime.datetime.combine(end_date, midnight).timestamp()))


def cycle_usage(billing_day):
    """What Airtel's own texts say has been used this cycle.

    Airtel's daily SMS is the figure reported now: it arrives whether or not
    this dashboard was running, so it does not have the "PC was off" holes the
    measured ledger has. The trade-off is a day's lag -- today's total does not
    text in until tomorrow -- so ``carrier_through`` says how current the
    figure actually is. The measured ledger (counter deltas, hourly) is kept as
    ``measured_used`` for cross-checking, and is what fills in until the first
    SMS of a new cycle arrives.

    ``covered_from`` is the honest part: if Airtel's texts only go back part-way
    through the cycle, the figure is a floor, and the dashboard says so rather
    than pretending the cycle began the day the first text was seen.
    """
    start_ts, end_ts = cycle_bounds(billing_day)
    first = tracked_since()
    now = int(time.time())

    reported = carrier_days(start_ts, now)
    carrier_used = sum(r["bytes"] or 0 for r in reported)
    measured_used = traffic_total(start_ts, now) if first else 0

    return {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "tracked_since": first,
        "covered_from": reported[0]["ts"] if reported else None,
        "complete": bool(reported and reported[0]["ts"] <= start_ts),
        "used": carrier_used if reported else measured_used,
        "source": "carrier" if reported else "measured",
        "measured_used": measured_used,
        "carrier_days": len(reported),
        "carrier_through": reported[-1]["day"] if reported else None,
        "elapsed_ratio": min(1.0, max(0.0, (now - start_ts) / (end_ts - start_ts))),
    }


def prune(retention_days):
    cutoff = int(time.time()) - retention_days * 86400
    with connect() as conn:
        for table in ("signal_samples", "usage_samples", "device_samples", "events"):
            conn.execute("DELETE FROM %s WHERE ts < ?" % table, (cutoff,))
        # The ledger is tiny -- one row per hour per source -- and it is the only
        # record of months gone by, so it is kept for far longer than the samples.
        conn.execute("DELETE FROM traffic WHERE hour < ?",
                     (int(time.time()) - 5 * 365 * 86400,))
