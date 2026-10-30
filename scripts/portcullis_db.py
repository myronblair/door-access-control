#!/usr/bin/env python3
"""Portcullis shared database layer — one schema, one attendance query, one camera fetch.

Used by collector.py (writes events + snapshots), ops.py (web app) and report.py (CLI), so the
tables and the report logic live in exactly one place.

Copyright (c) 2026 TomTom Enterprises.
"""
import os, re, sqlite3, subprocess, datetime, json

DB = os.environ.get("PORTCULLIS_DB", "/var/lib/portcullis/portcullis.db")

# uhppoted-httpd keeps its own authoritative stores here; the main web UI reads door/controller/card
# names from these files, so Portcullis reads the same ones rather than keeping a divergent copy.
HTTPD_SYS = os.environ.get("PORTCULLIS_HTTPD_SYS", "/var/uhppoted/httpd/system")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(idx INTEGER PRIMARY KEY, ts TEXT, card TEXT, door INTEGER,
    granted INTEGER, event_type INTEGER, raw TEXT, collected_at TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_card ON events(card);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS patrol_points(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, door INTEGER);
CREATE TABLE IF NOT EXISTS patrol_routes(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    doors TEXT, win_start TEXT, win_end TEXT);
CREATE TABLE IF NOT EXISTS meetings(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, door INTEGER,
    start_ts TEXT, end_ts TEXT);
CREATE TABLE IF NOT EXISTS cameras(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, ip TEXT,
    username TEXT, door INTEGER, rtsp TEXT, password TEXT, channel TEXT DEFAULT '101');
CREATE TABLE IF NOT EXISTS snapshots(idx INTEGER PRIMARY KEY, path TEXT, camera TEXT, taken_at TEXT);
"""


_schema_done = False


def connect(row_factory=None):
    """Open the database (creating/upgrading the schema once per process). The file is kept
    owner-only (it holds camera credentials)."""
    global _schema_done
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB, timeout=15)
    if row_factory:
        con.row_factory = row_factory
    if not _schema_done:                        # schema init is once-per-process, not per request
        con.executescript(_SCHEMA)
        cols = [r[1] for r in con.execute("PRAGMA table_info(cameras)")]
        if "password" not in cols: con.execute("ALTER TABLE cameras ADD COLUMN password TEXT")
        if "channel"  not in cols: con.execute("ALTER TABLE cameras ADD COLUMN channel TEXT DEFAULT '101'")
        con.commit()
        try:
            os.chmod(DB, 0o600)
        except OSError:
            pass
        _schema_done = True
    return con


CLI = "/usr/local/bin/uhppote-cli"


def cli(conf, *args, timeout=15):
    """Run uhppote-cli against a config; returns (returncode, combined output)."""
    r = subprocess.run([CLI, "--config", conf, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def configured_controllers(conf):
    """Serials already declared in uhppoted.conf -> IP."""
    out = {}
    try:
        for line in open(conf):
            m = re.match(r"\s*UT0311-L0x\.(\d+)\.address\s*=\s*([\d.]+)", line)
            if m:
                out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


def discover(conf):
    """Broadcast-discover UHPPOTE controllers on the LAN (get-devices). Returns a list of dicts."""
    _, out = cli(conf, "get-devices")
    found = []
    for line in out.splitlines():
        p = line.split()                           # serial ip mask gw mac version date
        if len(p) >= 2 and p[0].isdigit():
            found.append({"serial": p[0], "ip": p[1],
                          "mask": p[2] if len(p) > 2 else "", "gw": p[3] if len(p) > 3 else "",
                          "mac": p[4] if len(p) > 4 else "", "version": p[5] if len(p) > 5 else "",
                          "date": p[6] if len(p) > 6 else ""})
    return found


def _httpd_json(name):
    """Load one uhppoted-httpd system store (doors.json/controllers.json/cards.json), or {} if
    unreadable. These are the names the main Access UI shows — the single source of truth."""
    try:
        with open(os.path.join(HTTPD_SYS, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def httpd_controllers():
    """Controllers as the Access UI knows them: dicts {serial, name, address, doors{pos:door-OID}}."""
    out = []
    for c in _httpd_json("controllers.json").get("controllers", []):
        out.append({"serial": str(c.get("device-id") or ""), "name": (c.get("name") or "").strip(),
                    "address": c.get("address") or "", "doors": c.get("doors") or {}})
    return out


def _door_oid_names():
    return {d.get("OID"): (d.get("name") or "").strip()
            for d in _httpd_json("doors.json").get("doors", []) if d.get("OID")}


def door_names(serial=None):
    """{1..4: name} for a controller, resolved through the httpd store so names match the main UI.
    Positions with no door record fall back to 'Door N'. serial=None picks the first controller that
    has a device address (the real panel)."""
    oid_names, ctrls = _door_oid_names(), httpd_controllers()
    chosen = None
    if serial:
        chosen = next((c for c in ctrls if c["serial"] == str(serial)), None)
    if chosen is None:
        chosen = next((c for c in ctrls if c["address"]), None) or (ctrls[0] if ctrls else {})
    doors = chosen.get("doors", {}) if chosen else {}
    return {n: (oid_names.get(doors.get(str(n), ""), "") or f"Door {n}") for n in (1, 2, 3, 4)}


def card_names():
    """card-number (as text) -> holder name, from the Access UI's card store."""
    return {str(c.get("card")): (c.get("name") or "").strip()
            for c in _httpd_json("cards.json").get("cards", []) if c.get("card")}


def attendance(con, days=30):
    """Per-card, per-day first/last seen + counts over the last `days` days."""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT date(ts) d, card, MIN(ts) first_seen, MAX(ts) last_seen,
                  COUNT(*) events, COALESCE(SUM(granted),0) granted
           FROM events WHERE ts >= ?
           GROUP BY date(ts), card ORDER BY d DESC, card""", (since,)).fetchall()
    return [{"date": r[0], "card": r[1], "first_seen": r[2], "last_seen": r[3],
             "events": r[4], "granted": r[5]} for r in rows]


_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?(:\d{1,5})?$")


def valid_host(h):
    return bool(h) and len(h) <= 255 and bool(_HOST_RE.match(h))


def _q(v):
    """Quote a value for a curl --config file."""
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "").replace("\r", "") + '"'


def fetch_jpeg(ip, channel, user, password, timeout=8):
    """Grab a Hikvision ISAPI snapshot and return the JPEG bytes (or None). Credentials go to curl
    through its config on stdin, never on the command line where `ps` / /proc would show them."""
    if not valid_host(ip):
        return None
    ch = re.sub(r"\D", "", str(channel or "")) or "101"
    cfg = "\n".join([
        "silent", "digest", f"max-time = {int(timeout)}",
        f"user = {_q((user or 'admin') + ':' + (password or ''))}",
        f"url = {_q(f'http://{ip}/ISAPI/Streaming/channels/{ch}/picture')}",
    ]) + "\n"
    try:
        r = subprocess.run(["curl", "--config", "-"], input=cfg.encode(), capture_output=True,
                           timeout=timeout + 5)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode == 0 and len(r.stdout) > 1000 and r.stdout[:2] == b"\xff\xd8":
        return r.stdout
    return None
