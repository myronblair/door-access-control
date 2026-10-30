#!/usr/bin/env python3
"""Portcullis event collector — the Phase-B foundation.

Polls the controller's stored access events into a local SQLite database so the attendance,
patrol and meeting-sign modules have a durable event history to read. Polls the controller's
event *buffer* by index (not the UDP push), so it backfills history and survives the controller
flapping offline — a missed poll is simply retried next cycle.

  DB:     /var/lib/portcullis/portcullis.db   (table `events`)
  Poll:   every PORTCULLIS_POLL seconds (default 30)

Copyright (c) 2026 TomTom Enterprises.
"""
import sqlite3, subprocess, time, os, datetime

CONF   = os.environ.get("PORTCULLIS_CONF", "/etc/uhppoted/uhppoted.conf")
SERIAL = os.environ.get("PORTCULLIS_SERIAL", "223208728")
CLI    = "/usr/local/bin/uhppote-cli"
DB     = os.environ.get("PORTCULLIS_DB", "/var/lib/portcullis/portcullis.db")
POLL   = int(os.environ.get("PORTCULLIS_POLL", "30"))
SNAP_DIR     = os.environ.get("PORTCULLIS_SNAPSHOTS", "/var/lib/portcullis/snapshots")
SNAP_MAX_AGE = 180   # only snapshot fresh events (never the historical backfill)


def cli(*a, t=15):
    r = subprocess.run([CLI, "--config", CONF, *a], capture_output=True, text=True, timeout=t)
    return r.returncode, (r.stdout + r.stderr).strip()


def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS events(
        idx INTEGER PRIMARY KEY, ts TEXT, card TEXT, door INTEGER,
        granted INTEGER, event_type INTEGER, raw TEXT, collected_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_card ON events(card)")
    con.execute("""CREATE TABLE IF NOT EXISTS cameras(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
        ip TEXT, username TEXT, door INTEGER, rtsp TEXT, password TEXT, channel TEXT DEFAULT '101')""")
    con.execute("CREATE TABLE IF NOT EXISTS snapshots(idx INTEGER PRIMARY KEY, path TEXT, camera TEXT, taken_at TEXT)")
    cols = [r[1] for r in con.execute("PRAGMA table_info(cameras)")]
    if "password" not in cols: con.execute("ALTER TABLE cameras ADD COLUMN password TEXT")
    if "channel"  not in cols: con.execute("ALTER TABLE cameras ADD COLUMN channel TEXT DEFAULT '101'")
    return con


def maybe_snapshot(con, idx, door, ts):
    """If a camera is linked to this door, grab a Hikvision ISAPI snapshot (curl digest) for fresh
    events only. Stored under SNAP_DIR and recorded in `snapshots`. All config comes from the UI."""
    try:
        age = (datetime.datetime.now() - datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except ValueError:
        return
    if age > SNAP_MAX_AGE:
        return
    cam = con.execute("SELECT ip, username, password, channel FROM cameras WHERE door=? AND ip!='' LIMIT 1",
                      (door,)).fetchone()
    if not cam or not cam[0]:
        return
    ip, user, pw, ch = cam[0], cam[1] or "admin", cam[2] or "", cam[3] or "101"
    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, f"{idx}.jpg")
    url = f"http://{ip}/ISAPI/Streaming/channels/{ch}/picture"
    r = subprocess.run(["curl", "-s", "--max-time", "8", "--digest", "-u", f"{user}:{pw}", url, "-o", path],
                       capture_output=True)
    if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 1000:
        con.execute("INSERT OR REPLACE INTO snapshots(idx, path, camera, taken_at) VALUES(?,?,?,?)",
                    (idx, path, ip, datetime.datetime.now().isoformat(timespec="seconds")))
        con.commit()
    else:
        try: os.remove(path)
        except OSError: pass


def bounds():
    """Return (first, last) event indices on the controller, or None if unreachable."""
    rc, out = cli("get-events", SERIAL)          # e.g. "223208728 1 18 0"
    if rc != 0:
        return None
    p = out.split()
    try:
        return int(p[1]), int(p[2])
    except (IndexError, ValueError):
        return None


def parse(out):
    # "223208728 18 2023-07-12 18:01:19 22322431 1 true 1"
    p = out.split()
    if len(p) < 8:
        return None
    try:
        return (int(p[1]), p[2] + " " + p[3], p[4], int(p[5]),
                1 if p[6].lower() == "true" else 0, int(p[7]), out)
    except (ValueError, IndexError):
        return None


def poll(con):
    b = bounds()
    if not b:
        return 0
    first, last = b
    have = con.execute("SELECT MAX(idx) FROM events").fetchone()[0] or 0
    start = max(have + 1, first)
    n = 0
    fresh = []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for i in range(start, last + 1):
        rc, out = cli("get-event", SERIAL, str(i))
        if rc != 0:
            break                                 # controller dropped mid-poll; resume next cycle
        row = parse(out)
        if row:
            con.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?)", (*row, now))
            n += 1
            fresh.append((row[0], row[3], row[1]))   # idx, door, ts
    con.commit()                                   # durability first — don't hold events hostage to camera latency
    for idx, door, ts in fresh:                    # snapshots are best-effort, after commit
        try:
            maybe_snapshot(con, idx, door, ts)
        except Exception:
            pass
    return n


if __name__ == "__main__":
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = db()
    while True:
        try:
            poll(con)
        except Exception:
            pass
        time.sleep(POLL)
