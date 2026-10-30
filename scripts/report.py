#!/usr/bin/env python3
"""Portcullis attendance report (Phase B — attendance module).

Computes per-card, per-day first-seen / last-seen / counts from the events DB that the collector
maintains. This is the time-&-attendance core; patrol and meeting-sign build on the same events.

  report.py [--days N] [--json]
Copyright (c) 2026 TomTom Enterprises.
"""
import sys, json

import portcullis_db as pdb

if __name__ == "__main__":
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 30
    data = pdb.attendance(pdb.connect(), days)
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        print(f"{'DATE':12} {'CARD':12} {'FIRST SEEN':20} {'LAST SEEN':20} {'EVTS':>5} {'GRANT':>5}")
        print("-" * 78)
        for a in data:
            print(f"{a['date']:12} {a['card']:12} {a['first_seen']:20} {a['last_seen']:20} "
                  f"{a['events']:>5} {a['granted']:>5}")
        print(f"\n{len(data)} card-day rows over {days} days." if data else "(no events in range)")
