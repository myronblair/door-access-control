#!/usr/bin/env python3
"""Portcullis attendance report (Phase B — attendance module).

Computes per-card, per-day first-seen / last-seen / counts from the events DB that the collector
maintains. This is the time-&-attendance core; patrol and meeting-sign build on the same events.

  report.py [--days N] [--json]
Copyright (c) 2026 TomTom Enterprises.
"""
import sqlite3, os, sys, json, datetime

DB = os.environ.get("PORTCULLIS_DB", "/var/lib/portcullis/portcullis.db")


def attendance(days=30):
    con = sqlite3.connect(DB)
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT date(ts) d, card, MIN(ts) first_seen, MAX(ts) last_seen,
                  COUNT(*) events, COALESCE(SUM(granted),0) granted
           FROM events WHERE ts >= ?
           GROUP BY date(ts), card ORDER BY d DESC, card""", (since,)).fetchall()
    return [{"date": r[0], "card": r[1], "first_seen": r[2], "last_seen": r[3],
             "events": r[4], "granted": r[5]} for r in rows]


if __name__ == "__main__":
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 30
    data = attendance(days)
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        print(f"{'DATE':12} {'CARD':12} {'FIRST SEEN':20} {'LAST SEEN':20} {'EVTS':>5} {'GRANT':>5}")
        print("-" * 78)
        for a in data:
            print(f"{a['date']:12} {a['card']:12} {a['first_seen']:20} {a['last_seen']:20} "
                  f"{a['events']:>5} {a['granted']:>5}")
        print(f"\n{len(data)} card-day rows over {days} days." if data else "(no events in range)")
