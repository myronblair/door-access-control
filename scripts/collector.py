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
    return con


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
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for i in range(start, last + 1):
        rc, out = cli("get-event", SERIAL, str(i))
        if rc != 0:
            break                                 # controller dropped mid-poll; resume next cycle
        row = parse(out)
        if row:
            con.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?)", (*row, now))
            n += 1
    con.commit()
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
