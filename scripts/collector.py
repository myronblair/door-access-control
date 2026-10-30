#!/usr/bin/env python3
"""Portcullis event collector — the Phase-B foundation.

Polls the controller's stored access events into a local SQLite database so the attendance,
patrol and meeting-sign modules have a durable event history to read. Polls the controller's
event *buffer* by index (not the UDP push), so it backfills history and survives the controller
flapping offline — a missed poll is simply retried next cycle.

  DB:     /var/lib/portcullis/portcullis.db   (table `events`, schema in portcullis_db.py)
  Poll:   every PORTCULLIS_POLL seconds (default 10)

If the controller's event counter ever goes backwards (factory reset / replaced panel) the stored
indices are rebased (`meta.idx_base`) so new events keep being collected instead of colliding with
old ones. Camera snapshots are taken for fresh events only, judged against the *controller's*
clock, pruned after PORTCULLIS_SNAP_KEEP_DAYS (default 30).

Copyright (c) 2026 TomTom Enterprises.
"""
import subprocess, time, os, sys, re, datetime, traceback
from concurrent.futures import ThreadPoolExecutor

import portcullis_db as pdb

CONF   = os.environ.get("PORTCULLIS_CONF", "/etc/uhppoted/uhppoted.conf")
SERIAL = os.environ.get("PORTCULLIS_SERIAL", "223208728")
CLI    = "/usr/local/bin/uhppote-cli"
POLL   = int(os.environ.get("PORTCULLIS_POLL", "10"))
SNAP_DIR       = os.environ.get("PORTCULLIS_SNAPSHOTS", "/var/lib/portcullis/snapshots")
SNAP_MAX_AGE   = 180   # only snapshot fresh events (never the historical backfill)
SNAP_KEEP_DAYS = int(os.environ.get("PORTCULLIS_SNAP_KEEP_DAYS", "30"))
PRUNE_EVERY    = 3600


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def cli(*a, t=15):
    r = subprocess.run([CLI, "--config", CONF, *a], capture_output=True, text=True, timeout=t)
    return r.returncode, (r.stdout + r.stderr).strip()


def meta_get(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    con.commit()


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


def controller_skew():
    """Seconds the controller's clock is ahead of this host (0 if it can't be read)."""
    try:
        rc, out = cli("get-time", SERIAL, t=8)
        m = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", out)
        if rc == 0 and m:
            ctrl = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
            return (ctrl - datetime.datetime.now()).total_seconds()
    except Exception:
        pass
    return 0.0


def camera_map(con):
    """door -> (ip, user, password, channel) for the first camera linked to each door."""
    cams = {}
    for door, ip, user, pw, ch in con.execute(
            "SELECT door, ip, username, password, channel FROM cameras WHERE ip IS NOT NULL AND ip!=''"):
        cams.setdefault(door, (ip, user, pw, ch))
    return cams


def snapshot_fresh(con, fresh):
    """Best-effort ISAPI snapshots for fresh events on doors that have a camera. The captures run
    concurrently (network I/O only); all database writes stay on this thread."""
    cams = camera_map(con)
    cand = [f for f in fresh if f[1] in cams]
    if not cand:
        return
    skew, now = controller_skew(), datetime.datetime.now()

    def age(ts):
        try:
            return (now - datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds() + skew
        except ValueError:
            return None

    cand = [f for f in cand if (age(f[2]) is not None and age(f[2]) <= SNAP_MAX_AGE)]
    if not cand:
        return
    def shot(f):
        ip, user, pw, ch = cams[f[1]]
        return pdb.fetch_jpeg(ip, ch, user, pw)

    with ThreadPoolExecutor(max_workers=4) as ex:
        shots = list(ex.map(shot, cand))
    os.makedirs(SNAP_DIR, mode=0o700, exist_ok=True)
    for (idx, door, ts), jpg in zip(cand, shots):
        if not jpg:
            continue
        path = os.path.join(SNAP_DIR, f"{idx}.jpg")
        with open(path, "wb") as fh:
            fh.write(jpg)
        con.execute("INSERT OR REPLACE INTO snapshots(idx, path, camera, taken_at) VALUES(?,?,?,?)",
                    (idx, path, cams[door][0], datetime.datetime.now().isoformat(timespec="seconds")))
    con.commit()


def prune(con):
    """Drop snapshot files + rows older than SNAP_KEEP_DAYS so the disk never fills."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=SNAP_KEEP_DAYS)).isoformat(timespec="seconds")
    old = con.execute("SELECT idx, path FROM snapshots WHERE taken_at < ?", (cutoff,)).fetchall()
    for idx, path in old:
        try: os.remove(path)
        except OSError: pass
        con.execute("DELETE FROM snapshots WHERE idx=?", (idx,))
    con.commit()
    return len(old)


def poll(con):
    b = bounds()
    if not b:
        return 0
    first, last = b
    base = int(meta_get(con, "idx_base", "0"))
    top  = con.execute("SELECT MAX(idx) FROM events").fetchone()[0] or 0
    have = top - base                              # highest controller index already stored
    if last < have:                                # controller counter went backwards (reset/replaced)
        log(f"controller event counter went backwards ({last} < {have}); rebasing stored indices")
        base = top
        meta_set(con, "idx_base", base)
        have = 0
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
            row = (row[0] + base, *row[1:])
            con.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?)", (*row, now))
            n += 1
            fresh.append((row[0], row[3], row[1]))   # idx, door, ts
    con.commit()                                   # durability first — don't hold events hostage to camera latency
    if fresh:
        try:
            snapshot_fresh(con, fresh)             # best-effort, after commit
        except Exception:
            log("snapshot error:\n" + traceback.format_exc())
    return n


if __name__ == "__main__":
    con = pdb.connect()
    last_prune = 0.0
    while True:
        try:
            poll(con)
            if time.time() - last_prune > PRUNE_EVERY:
                prune(con)
                last_prune = time.time()
        except Exception:
            log("collector error:\n" + traceback.format_exc())
        time.sleep(POLL)
