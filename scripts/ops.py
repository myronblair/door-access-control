#!/usr/bin/env python3
"""Portcullis Ops — Phase-B admin web app.

Pages to view events and run the attendance / patrol / meeting modules, plus settings pages to
configure each (patrol checkpoints & routes, meetings, cameras) without code changes. Reads the
event history the collector maintains in SQLite and keeps its own config in the same DB.

Runs on :8083 behind nginx at /ops (same Cloudflare tunnel / Access gate as the main UI). Flask app.

Security:
  * every request must carry the Cloudflare Access identity header (Cf-Access-Authenticated-User-Email),
    optionally restricted to PORTCULLIS_OPS_ALLOW (comma-separated emails); PORTCULLIS_OPS_AUTH=off disables;
  * every POST must come from this same host (Origin/Referer check) — no cross-site form posts;
  * all stored text is HTML-escaped on output.

Copyright (c) 2026 TomTom Enterprises.
"""
import os, re, datetime, subprocess
from urllib.parse import urlsplit
from flask import Flask, request, redirect, url_for, render_template_string, send_file, g, abort
from markupsafe import escape

import portcullis_db as pdb

CONF = os.environ.get("PORTCULLIS_CONF", "/etc/uhppoted/uhppoted.conf")

SNAP_DIR = os.path.realpath(os.environ.get("PORTCULLIS_SNAPSHOTS", "/var/lib/portcullis/snapshots"))
AUTH     = os.environ.get("PORTCULLIS_OPS_AUTH", "access").lower()
ALLOW    = {e.strip().lower() for e in os.environ.get("PORTCULLIS_OPS_ALLOW", "").split(",") if e.strip()}
app = Flask(__name__)


def h(v):
    """HTML-escape any value for interpolation into the page body."""
    return str(escape("" if v is None else v))


@app.before_request
def gate():
    if AUTH != "off":
        who = request.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()
        if not who or (ALLOW and who not in ALLOW):
            abort(403)
    if request.method == "POST":                       # CSRF: reject only explicit cross-origin posts
        src = request.headers.get("Origin") or request.headers.get("Referer")
        if src and urlsplit(src).hostname != request.host.split(":")[0]:
            abort(403)


def db():
    """One connection per request (closed on teardown). Door and card-holder names come from the
    uhppoted-httpd store — the same source the main Access UI uses — so labels never drift."""
    if "db" not in g:
        g.db = pdb.connect(pdb.sqlite3.Row)
        g.door_map = pdb.door_names()
        g.card_map = pdb.card_names()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    c = g.pop("db", None)
    if c is not None:
        c.close()


def door_name(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return f"Door {h(n)}"
    return h(g.door_map.get(n, f"Door {n}"))


def card_label(num):
    """Show the holder's name (from the Access UI) with the raw number, or just the number."""
    name = g.card_map.get(str(num))
    return f"{h(name)} <span class=muted>{h(num)}</span>" if name else h(num)


def parse_days(default=30):
    try:
        return min(max(int(request.args.get("days", default)), 1), 3650)
    except ValueError:
        return default


def parse_doors(csv):
    """'1, 2,x,9' -> [1, 2] (ignores anything that isn't a door 1-4)."""
    out = []
    for d in (csv or "").split(","):
        d = d.strip()
        if d.isdigit() and 1 <= int(d) <= 4 and int(d) not in out:
            out.append(int(d))
    return out


def valid_door(v):
    return str(v).isdigit() and 1 <= int(v) <= 4


def norm_hhmm(s, default):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (s or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return default
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def norm_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime((s or "").strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def route_window(start, end, now):
    """The current/most recent run of a patrol route as full timestamps. A window whose start is
    later than its end (22:00-06:00) runs overnight: tonight's if it has begun, else last night's."""
    s, e = norm_hhmm(start, "00:00"), norm_hhmm(end, "23:59")
    today, day = now.date(), datetime.timedelta(days=1)
    if s <= e:
        a, b = today, today
    elif now.strftime("%H:%M") >= s:
        a, b = today, today + day
    else:
        a, b = today - day, today
    return f"{a.isoformat()} {s}:00", f"{b.isoformat()} {e}:59"


BASE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Portcullis Ops · {{title}}</title>
<style>
:root{--bg:#0f141b;--panel:#161d27;--line:#2a3542;--brass:#e0b23b;--text:#e6edf3;--muted:#8b98a5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 'Lato',system-ui,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;background:linear-gradient(90deg,#0b0f14,#161d27);border-bottom:2px solid var(--brass)}
header b{font-size:20px;font-weight:800;letter-spacing:.5px;color:var(--brass)}
nav{display:flex;flex-wrap:wrap;gap:4px;padding:10px 16px;background:#0b0f14;border-bottom:1px solid var(--line)}
nav a{color:var(--muted);text-decoration:none;padding:7px 13px;border-radius:8px;font-weight:700}
nav a:hover{color:var(--text);background:#161d27}nav a.on{color:#0f141b;background:var(--brass)}
main{max-width:1000px;margin:22px auto;padding:0 16px}
h1{font-size:22px;color:var(--brass);margin:0 0 16px}h2{font-size:16px;color:var(--brass);margin:26px 0 10px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}th{color:var(--brass);text-transform:uppercase;font-size:12px;letter-spacing:.5px}
tr:last-child td{border-bottom:none}.g{color:#2ec77a}.r{color:#e5484d}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
form.row{display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin-top:10px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:3px}
input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
button{background:var(--brass);color:#0f141b;border:none;border-radius:8px;padding:9px 16px;font-weight:800;cursor:pointer}
button.del{background:#3a1f22;color:#e5484d;padding:5px 10px;font-weight:700}
.tiles{display:flex;flex-wrap:wrap;gap:14px}.tile{flex:1;min-width:150px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.tile .n{font-size:30px;font-weight:800;color:var(--brass)}.tile .l{color:var(--muted);font-size:13px}
.muted{color:var(--muted)}.err{background:#3a1f22;color:#ff8a8e;border-radius:8px;padding:9px 12px}
</style></head><body>
<header><b>&#9113; PORTCULLIS OPS</b><span class=muted>access events &middot; attendance &middot; patrol &middot; meetings</span>
<a href="/" style="margin-left:auto;color:var(--muted);text-decoration:none">&#8592; Access UI</a></header>
<nav>
{% for p,l in pages %}<a href="{{url_for(p)}}" class="{{'on' if active==p else ''}}">{{l}}</a>{% endfor %}
</nav><main>{% if err %}<p class=err>{{err}}</p>{% endif %}{{ body|safe }}</main></body></html>"""

PAGES = [("dashboard", "Dashboard"), ("events", "Events"), ("attendance", "Attendance"),
         ("patrol", "Patrol"), ("meetings", "Meetings"), ("cameras", "Cameras"),
         ("devices", "Devices"), ("controllers", "Controllers"), ("settings", "Settings")]


def page(active, title, body):
    return render_template_string(BASE, pages=PAGES, active=active, title=title, body=body,
                                  err=request.args.get("err", ""))


def bad(endpoint, msg):
    return redirect(url_for(endpoint, err=msg))


def delete_form(field, ident):
    return (f"<form method=post style=display:inline><input type=hidden name={field} value={int(ident)}>"
            f"<button class=del>delete</button></form>")


@app.route("/")
def dashboard():
    c = db()
    total = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
    cards = c.execute("SELECT COUNT(DISTINCT card) n FROM events").fetchone()["n"]
    last  = c.execute("SELECT MAX(ts) t FROM events").fetchone()["t"] or "—"
    recent = c.execute("SELECT * FROM events ORDER BY idx DESC LIMIT 10").fetchall()
    rows = "".join(f"<tr><td>{h(e['ts'])}</td><td>{card_label(e['card'])}</td><td>{door_name(e['door'])}</td>"
                   f"<td class='{'g' if e['granted'] else 'r'}'>{'granted' if e['granted'] else 'denied'}</td></tr>"
                   for e in recent) or "<tr><td colspan=4 class=muted>no events yet</td></tr>"
    body = f"""<h1>Dashboard</h1>
    <div class=tiles>
      <div class=tile><div class=n>{total}</div><div class=l>total events</div></div>
      <div class=tile><div class=n>{cards}</div><div class=l>distinct cards</div></div>
      <div class=tile><div class=n style='font-size:16px'>{h(last)}</div><div class=l>last event</div></div>
    </div>
    <h2>Recent activity</h2><table><tr><th>Time</th><th>Card</th><th>Door</th><th>Result</th></tr>{rows}</table>"""
    return page("dashboard", "Dashboard", body)


@app.route("/events")
def events():
    c = db(); days = parse_days()
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    evs = c.execute("""SELECT e.*, s.idx AS snap FROM events e LEFT JOIN snapshots s ON s.idx=e.idx
                       WHERE e.ts>=? ORDER BY e.idx DESC LIMIT 500""", (since,)).fetchall()
    cam = lambda e: (f"<a href='{url_for('snapshot', idx=e['idx'])}' target=_blank title='camera snapshot'>&#128247;</a>" if e['snap'] else "")
    rows = "".join(f"<tr><td>{e['idx']}</td><td>{h(e['ts'])}</td><td>{card_label(e['card'])}</td><td>{door_name(e['door'])}</td>"
                   f"<td class='{'g' if e['granted'] else 'r'}'>{'granted' if e['granted'] else 'denied'}</td>"
                   f"<td>{h(e['event_type'])}</td><td>{cam(e)}</td></tr>" for e in evs) or "<tr><td colspan=7 class=muted>none</td></tr>"
    body = f"""<h1>Events</h1>
    <form class=row method=get><div><label>show last (days)</label><input name=days value="{days}" size=5></div><button>Filter</button></form>
    <p class=muted>{len(evs)} events (most recent 500)</p>
    <table><tr><th>#</th><th>Time</th><th>Card</th><th>Door</th><th>Result</th><th>Type</th><th>Cam</th></tr>{rows}</table>"""
    return page("events", "Events", body)


@app.route("/attendance")
def attendance():
    c = db(); days = parse_days()
    rows = "".join(f"<tr><td>{h(r['date'])}</td><td>{card_label(r['card'])}</td><td>{h(r['first_seen'])}</td>"
                   f"<td>{h(r['last_seen'])}</td><td>{r['events']}</td><td>{r['granted']}</td></tr>"
                   for r in pdb.attendance(c, days)) or "<tr><td colspan=6 class=muted>none</td></tr>"
    body = f"""<h1>Attendance</h1>
    <form class=row method=get><div><label>last (days)</label><input name=days value="{days}" size=5></div><button>Run</button></form>
    <table><tr><th>Date</th><th>Card</th><th>First seen</th><th>Last seen</th><th>Events</th><th>Granted</th></tr>{rows}</table>"""
    return page("attendance", "Attendance", body)


@app.route("/patrol", methods=["GET", "POST"])
def patrol():
    c = db()
    if request.method == "POST":
        f = request.form
        if f.get("add") == "point":
            name = f.get("name", "").strip()
            if not name or not valid_door(f.get("door")):
                return bad("patrol", "Checkpoint needs a name and a door 1-4.")
            c.execute("INSERT INTO patrol_points(name,door) VALUES(?,?)", (name, int(f["door"])))
        elif f.get("add") == "route":
            name, doors = f.get("name", "").strip(), parse_doors(f.get("doors"))
            if not name or not doors:
                return bad("patrol", "Route needs a name and doors 1-4 separated by commas (e.g. 1,2).")
            for k in ("win_start", "win_end"):
                if f.get(k, "").strip() and norm_hhmm(f[k], None) is None:
                    return bad("patrol", "Times must look like 22:00 (HH:MM).")
            c.execute("INSERT INTO patrol_routes(name,doors,win_start,win_end) VALUES(?,?,?,?)",
                      (name, ",".join(map(str, doors)), norm_hhmm(f.get("win_start"), "00:00"),
                       norm_hhmm(f.get("win_end"), "23:59")))
        elif f.get("del_point", "").isdigit():
            c.execute("DELETE FROM patrol_points WHERE id=?", (f["del_point"],))
        elif f.get("del_route", "").isdigit():
            c.execute("DELETE FROM patrol_routes WHERE id=?", (f["del_route"],))
        c.commit()
        return redirect(url_for("patrol"))
    pts = c.execute("SELECT * FROM patrol_points ORDER BY name").fetchall()
    rts = c.execute("SELECT * FROM patrol_routes ORDER BY name").fetchall()
    dsel = "".join(f"<option value={n}>{door_name(n)}</option>" for n in (1, 2, 3, 4))
    prows = "".join(f"<tr><td>{h(p['name'])}</td><td>{door_name(p['door'])}</td><td>{delete_form('del_point', p['id'])}</td></tr>"
                    for p in pts) or "<tr><td colspan=3 class=muted>no checkpoints</td></tr>"
    now = datetime.datetime.now()
    rrows = ""
    for r in rts:
        a, b = route_window(r["win_start"], r["win_end"], now)
        hits = []
        for d in parse_doors(r["doors"]):
            n = c.execute("SELECT COUNT(*) n FROM events WHERE door=? AND ts>=? AND ts<=?", (d, a, b)).fetchone()["n"]
            hits.append(f"<span class='{'g' if n else 'r'}'>{door_name(d)}{'✓' if n else '✗'}</span>")
        rrows += (f"<tr><td>{h(r['name'])}</td><td>{h(norm_hhmm(r['win_start'], '00:00'))}–{h(norm_hhmm(r['win_end'], '23:59'))}</td>"
                  f"<td>{' '.join(hits) or '<span class=muted>no valid doors</span>'}</td><td>{delete_form('del_route', r['id'])}</td></tr>")
    rrows = rrows or "<tr><td colspan=4 class=muted>no routes</td></tr>"
    body = f"""<h1>Patrol</h1>
    <div class=card><h2>Checkpoints</h2><table><tr><th>Name</th><th>Reader/Door</th><th></th></tr>{prows}</table>
      <form class=row method=post><input type=hidden name=add value=point>
        <div><label>name</label><input name=name required></div>
        <div><label>door</label><select name=door>{dsel}</select></div><button>Add checkpoint</button></form></div>
    <div class=card><h2>Routes (status for the current run)</h2><table><tr><th>Route</th><th>Window</th><th>Checkpoints hit</th><th></th></tr>{rrows}</table>
      <form class=row method=post><input type=hidden name=add value=route>
        <div><label>name</label><input name=name required></div>
        <div><label>doors (csv e.g. 1,2)</label><input name=doors placeholder="1,2"></div>
        <div><label>from</label><input name=win_start placeholder="22:00" size=6></div>
        <div><label>to</label><input name=win_end placeholder="06:00" size=6></div><button>Add route</button></form>
      <p class=muted>A window that ends before it starts (22:00 &ndash; 06:00) runs overnight.</p></div>"""
    return page("patrol", "Patrol", body)


@app.route("/meetings", methods=["GET", "POST"])
def meetings():
    c = db()
    if request.method == "POST":
        f = request.form
        if f.get("del", "").isdigit():
            c.execute("DELETE FROM meetings WHERE id=?", (f["del"],))
        else:
            name, start, end = f.get("name", "").strip(), norm_ts(f.get("start_ts")), norm_ts(f.get("end_ts"))
            if not name or not valid_door(f.get("door")) or not start or not end or end < start:
                return bad("meetings", "Meeting needs a name, a door, and start/end like 2026-10-30 09:00 (end after start).")
            c.execute("INSERT INTO meetings(name,door,start_ts,end_ts) VALUES(?,?,?,?)", (name, int(f["door"]), start, end))
        c.commit(); return redirect(url_for("meetings"))
    ms = c.execute("SELECT * FROM meetings ORDER BY start_ts DESC").fetchall()
    dsel = "".join(f"<option value={n}>{door_name(n)}</option>" for n in (1, 2, 3, 4))
    rows = ""
    for m in ms:
        att = c.execute("SELECT COUNT(DISTINCT card) n FROM events WHERE door=? AND ts>=? AND ts<=? AND granted=1",
                        (m["door"], m["start_ts"], m["end_ts"])).fetchone()["n"]
        rows += (f"<tr><td>{h(m['name'])}</td><td>{door_name(m['door'])}</td><td>{h(m['start_ts'])}</td><td>{h(m['end_ts'])}</td>"
                 f"<td class=g>{att} signed in</td><td>{delete_form('del', m['id'])}</td></tr>")
    rows = rows or "<tr><td colspan=6 class=muted>no meetings</td></tr>"
    body = f"""<h1>Meetings</h1>
    <table><tr><th>Name</th><th>Reader</th><th>Start</th><th>End</th><th>Attendance</th><th></th></tr>{rows}</table>
    <div class=card><h2>Add meeting</h2><form class=row method=post>
      <div><label>name</label><input name=name required></div>
      <div><label>reader/door</label><select name=door>{dsel}</select></div>
      <div><label>start (YYYY-MM-DD HH:MM)</label><input name=start_ts size=20 required></div>
      <div><label>end</label><input name=end_ts size=20 required></div><button>Add</button></form>
      <p class=muted>Attendance = distinct cards granted at that reader within the window.</p></div>"""
    return page("meetings", "Meetings", body)


@app.route("/cameras", methods=["GET", "POST"])
def cameras():
    c = db()
    if request.method == "POST":
        f = request.form
        if f.get("del", "").isdigit():
            c.execute("DELETE FROM cameras WHERE id=?", (f["del"],))
        else:
            name, ip, ch = f.get("name", "").strip(), f.get("ip", "").strip(), f.get("channel", "").strip() or "101"
            if not name or not pdb.valid_host(ip) or not ch.isdigit() or not valid_door(f.get("door")):
                return bad("cameras", "Camera needs a name, a valid IP/hostname, a numeric channel and a door.")
            c.execute("INSERT INTO cameras(name,ip,username,password,door,channel,rtsp) VALUES(?,?,?,?,?,?,?)",
                      (name, ip, f.get("username", "").strip() or "admin", f.get("password", ""),
                       int(f["door"]), ch, f.get("rtsp", "").strip()))
        c.commit(); return redirect(url_for("cameras"))
    cams = c.execute("SELECT * FROM cameras ORDER BY name").fetchall()
    dsel = "".join(f"<option value={n}>{door_name(n)}</option>" for n in (1, 2, 3, 4))
    rows = "".join(
        f"<tr><td>{h(m['name'])}</td><td>{h(m['ip'])}</td><td>{h(m['username'])}</td>"
        f"<td>{'&#9679; set' if m['password'] else '&mdash;'}</td><td>{h(m['channel'] or '101')}</td>"
        f"<td>{door_name(m['door'])}</td>"
        f"<td><form method=post action='{url_for('camera_test', cid=m['id'])}' target=_blank style=display:inline>"
        f"<button class=del>test</button></form></td>"
        f"<td>{delete_form('del', m['id'])}</td></tr>" for m in cams) or "<tr><td colspan=8 class=muted>no cameras</td></tr>"
    body = f"""<h1>Cameras</h1>
    <p class=muted>Hikvision ISAPI. A camera linked to a door gets an auto-snapshot when that door fires an event (shown on the Events page with &#128247;). Snapshots are kept for 30 days.</p>
    <table><tr><th>Name</th><th>IP</th><th>User</th><th>Pass</th><th>Ch</th><th>Door</th><th>Test</th><th></th></tr>{rows}</table>
    <div class=card><h2>Add camera</h2><form class=row method=post>
      <div><label>name</label><input name=name required></div>
      <div><label>IP</label><input name=ip placeholder="10.48.200.x" required></div>
      <div><label>username</label><input name=username value="admin"></div>
      <div><label>password</label><input name=password type=password></div>
      <div><label>channel</label><input name=channel value="101" size=5></div>
      <div><label>door</label><select name=door>{dsel}</select></div>
      <div><label>RTSP path (optional)</label><input name=rtsp placeholder="/Streaming/Channels/101"></div>
      <button>Add</button></form>
      <p class=muted>Snapshot URL: http://&lt;ip&gt;/ISAPI/Streaming/channels/&lt;ch&gt;/picture (digest auth). Click &ldquo;test&rdquo; after adding to verify the credentials.</p></div>"""
    return page("cameras", "Cameras", body)


@app.route("/snapshot/<int:idx>")
def snapshot(idx):
    r = db().execute("SELECT path FROM snapshots WHERE idx=?", (idx,)).fetchone()
    if r and r["path"]:
        p = os.path.realpath(r["path"])
        if p.startswith(SNAP_DIR + os.sep) and os.path.exists(p):   # only ever serve files under SNAP_DIR
            return send_file(p, mimetype="image/jpeg")
    return "no snapshot", 404


@app.route("/cameras/test/<int:cid>", methods=["POST"])
def camera_test(cid):
    m = db().execute("SELECT * FROM cameras WHERE id=?", (cid,)).fetchone()
    if not m or not m["ip"]:
        return "camera not found", 404
    jpg = pdb.fetch_jpeg(m["ip"], m["channel"], m["username"], m["password"])
    if jpg:
        return app.response_class(jpg, mimetype="image/jpeg")
    return "snapshot failed — check IP / credentials / channel", 502


@app.route("/settings")
def settings():
    db()
    rows = "".join(f"<tr><td>Door {n}</td><td>{door_name(n)}</td></tr>" for n in (1, 2, 3, 4))
    body = f"""<h1>Settings</h1>
    <div class=card><h2>Door names</h2>
      <table><tr><th>Position</th><th>Name</th></tr>{rows}</table>
      <p class=muted>Door names are read live from the Access UI's store, so Ops always matches it.
      Rename a door in the <a href="/" style="color:var(--brass)">Access UI</a> (Doors) and it updates
      everywhere here — Events, Attendance, Patrol and Meetings — automatically.</p></div>
    <div class=card><h2>Listener guard (anti phone-home)</h2>
      <p class=muted>Controlled by the door-control service on :8082 — <code>/guard/on</code>, <code>/guard/off</code>, <code>/guard/enforce</code> (Bearer token). Currently enforced to this host.</p></div>"""
    return page("settings", "Settings", body)


def known_controllers():
    """serial -> label ('name (serial)'), merging the Access UI store and the daemon config."""
    out = {}
    for c in pdb.httpd_controllers():
        if c["serial"]:
            out[c["serial"]] = f"{c['name'] or 'controller'} ({c['serial']})"
    for s in pdb.configured_controllers(CONF):
        out.setdefault(s, f"controller ({s})")
    return out


@app.route("/devices", methods=["GET", "POST"])
def devices():
    c = db()
    if request.method == "POST":
        f = request.form
        if f.get("del", "").isdigit():
            c.execute("DELETE FROM peripherals WHERE id=?", (f["del"],))
            c.commit(); return redirect(url_for("devices"))
        if f.get("addcam", "").strip():                      # promote a discovered camera to the Cameras page
            ip = f.get("addcam", "").strip()
            if pdb.valid_host(ip):
                c.execute("INSERT INTO cameras(name,ip,username,password,door,channel,rtsp) VALUES(?,?,?,?,?,?,?)",
                          (ip, ip, "admin", "", 1, "101", ""))
                c.commit()
            return redirect(url_for("cameras", err="Camera added with defaults — set its password and door on this page."))
        kind = f.get("kind", "").strip().lower()
        name = f.get("name", "").strip()
        ctrl = f.get("controller", "").strip()
        door = f.get("door", "").strip()
        ip   = f.get("ip", "").strip()
        if kind not in pdb.PERIPHERAL_KINDS or not name:
            return bad("devices", "An item needs a name and a valid type.")
        if door and not valid_door(door):
            return bad("devices", "Door must be 1-4 (or left blank).")
        if ctrl and not ctrl.isdigit():
            return bad("devices", "Controller must be a serial number.")
        if ip and not pdb.valid_host(ip):
            return bad("devices", "IP/host is not valid.")
        c.execute("INSERT INTO peripherals(kind,controller,door,name,model,ip,notes,created) VALUES(?,?,?,?,?,?,?,?)",
                  (kind, ctrl, int(door) if door else None, name[:60], f.get("model", "").strip()[:60],
                   ip, f.get("notes", "").strip()[:200], datetime.datetime.now().isoformat(timespec="seconds")))
        c.commit(); return redirect(url_for("devices"))

    kctrl = known_controllers()
    rows = ""
    # cameras (managed on the Cameras page) — shown here for a complete inventory
    for m in c.execute("SELECT * FROM cameras ORDER BY name"):
        rows += (f"<tr><td>Camera</td><td>{h(m['name'])}</td><td class=muted>—</td><td>{door_name(m['door'])}</td>"
                 f"<td>{h(m['ip'])}</td><td class=muted>manage on Cameras page</td><td></td></tr>")
    # keypads — read live from the controller's door flags (Access UI)
    for n in sorted(pdb.door_keypads()):
        rows += (f"<tr><td>Keypad</td><td>{door_name(n)} keypad</td><td class=muted>—</td><td>{door_name(n)}</td>"
                 f"<td class=muted>—</td><td class=muted>from Access UI door settings</td><td></td></tr>")
    # manual inventory items
    for p in c.execute("SELECT * FROM peripherals ORDER BY kind, name"):
        loc = kctrl.get(p["controller"] or "", h(p["controller"]) if p["controller"] else "<span class=muted>—</span>")
        meta = " · ".join(x for x in (h(p["model"]) if p["model"] else "", h(p["ip"]) if p["ip"] else "") if x) or "<span class=muted>—</span>"
        rows += (f"<tr><td>{h(p['kind'].title())}</td><td>{h(p['name'])}</td><td>{loc}</td>"
                 f"<td>{door_name(p['door']) if p['door'] else '<span class=muted>—</span>'}</td>"
                 f"<td>{meta}</td><td>{h(p['notes'])}</td><td>{delete_form('del', p['id'])}</td></tr>")
    rows = rows or "<tr><td colspan=7 class=muted>no devices yet</td></tr>"

    ksel = "".join(f"<option value={k}>{k.title()}</option>" for k in pdb.PERIPHERAL_KINDS if k != "keypad")
    csel = "<option value=''>— none —</option>" + "".join(
        f"<option value={escape(s)}>{escape(l)}</option>" for s, l in sorted(kctrl.items()))
    dsel = "<option value=''>— n/a —</option>" + "".join(f"<option value={n}>{door_name(n)}</option>" for n in (1, 2, 3, 4))

    disc = pdb.discover_onvif() if request.args.get("discover") else None
    disc_html = "<p class=muted>Scan the LAN for ONVIF/IP cameras. Keypads, locks and closers are wired to the controller and are not network-discoverable — add those below.</p>"
    if disc is not None:
        have = {m["ip"] for m in c.execute("SELECT ip FROM cameras")}
        drows = ""
        for d in disc:
            act = ("<span class=muted>already a camera</span>" if d["ip"] in have else
                   f"<form method=post style=display:inline><input type=hidden name=addcam value='{escape(d['ip'])}'><button>Add as camera</button></form>")
            drows += f"<tr><td>{escape(d['ip'])}</td><td class=muted>{escape(d.get('xaddr',''))}</td><td>{act}</td></tr>"
        drows = drows or "<tr><td colspan=3 class=muted>no cameras answered the ONVIF probe</td></tr>"
        disc_html = "<table><tr><th>IP</th><th>ONVIF endpoint</th><th></th></tr>" + drows + "</table>"

    body = f"""<h1>Devices</h1>
    <p class=muted>Every camera, keypad, lock, closer, reader and sensor tied to your controllers, in one inventory.</p>
    <table><tr><th>Type</th><th>Name</th><th>Controller</th><th>Door</th><th>Model / IP</th><th>Notes</th><th></th></tr>{rows}</table>
    <div class=card><h2>Discover cameras on the network</h2>
      <form method=get><input type=hidden name=discover value=1><button>Scan for cameras (ONVIF)</button></form>
      {disc_html}</div>
    <div class=card><h2>Add item</h2><form class=row method=post>
      <div><label>type</label><select name=kind>{ksel}</select></div>
      <div><label>name</label><input name=name required></div>
      <div><label>controller</label><select name=controller>{csel}</select></div>
      <div><label>door</label><select name=door>{dsel}</select></div>
      <div><label>make / model</label><input name=model></div>
      <div><label>IP (optional)</label><input name=ip placeholder="10.48.200.x"></div>
      <div><label>notes</label><input name=notes></div>
      <button>Add</button></form>
      <p class=muted>Cameras are added from the scan above (or the Cameras page) so their credentials can be tested;
      keypads come from the Access UI door settings automatically.</p></div>"""
    return page("devices", "Devices", body)


@app.route("/controllers", methods=["GET", "POST"])
def controllers():
    if request.method == "POST":                       # add a discovered controller
        serial, ip = request.form.get("serial", "").strip(), request.form.get("ip", "").strip()
        if serial.isdigit() and re.match(r"^[0-9.]+$", ip):
            try:
                subprocess.run(["sudo", "/usr/local/sbin/portcullis-add-controller", serial, ip], timeout=40)
            except Exception:
                pass
        return redirect(url_for("controllers"))
    cfg = pdb.configured_controllers(CONF)                 # daemon config (REST/CLI/collector/guard)
    ui  = {c["serial"]: c for c in pdb.httpd_controllers() if c["serial"]}  # Access UI store (authoritative names)
    dmap = {c["serial"]: pdb.door_names(c["serial"]) for c in ui.values()}
    crows = ""
    for s in sorted(set(cfg) | set(ui)):
        c = ui.get(s, {})
        name = c.get("name") or "<span class=muted>(unnamed)</span>"
        ip = c.get("address") or cfg.get(s, "")
        doors = dmap.get(s) or {}
        dtxt = ", ".join(f"{n}:{escape(doors[n])}" for n in (1, 2, 3, 4) if doors.get(n, "").strip()
                         and not doors[n].startswith("Door ")) or "<span class=muted>—</span>"
        flags = []
        flags.append("<span class=g>Access&nbsp;UI</span>" if s in ui else "<span class=r>not in Access&nbsp;UI</span>")
        flags.append("<span class=g>daemons</span>" if s in cfg else "<span class=r>not in config</span>")
        crows += (f"<tr><td>{name}</td><td>{escape(s)}</td><td>{escape(ip)}</td>"
                  f"<td>{dtxt}</td><td>{' · '.join(flags)}</td></tr>")
    crows = crows or "<tr><td colspan=5 class=muted>none</td></tr>"
    disc = pdb.discover(CONF) if request.args.get("discover") else None
    disc_html = "<p class=muted>Click to broadcast-scan the LAN for UHPPOTE controllers.</p>"
    if disc is not None:
        drows = ""
        for d in disc:
            new = d["serial"] not in cfg
            act = ((f"<form method=post style=display:inline>"
                    f"<input type=hidden name=serial value='{escape(d['serial'])}'>"
                    f"<input type=hidden name=ip value='{escape(d['ip'])}'><button>Add</button></form>")
                   if new else "<span class=muted>already added</span>")
            drows += (f"<tr><td>{escape(d['serial'])}</td><td>{escape(d['ip'])}</td>"
                      f"<td>{escape(d['version'])}</td><td>{escape(d['mac'])}</td>"
                      f"<td class='{'r' if new else 'g'}'>{'NEW' if new else 'configured'}</td><td>{act}</td></tr>")
        drows = drows or "<tr><td colspan=6 class=muted>no controllers answered the broadcast</td></tr>"
        disc_html = ("<table><tr><th>Serial</th><th>IP</th><th>Version</th><th>MAC</th>"
                     "<th>Status</th><th></th></tr>" + drows + "</table>")
    body = f"""<h1>Controllers</h1>
    <div class=card><h2>Known controllers</h2>
      <table><tr><th>Name</th><th>Serial</th><th>IP</th><th>Named doors</th><th>Registered in</th></tr>{crows}</table>
      <p class=muted>Names and door assignments come from the Access UI store (the single source of truth).
      &ldquo;daemons&rdquo; means it is in the service config that the REST API, collector and guard use;
      &ldquo;Access&nbsp;UI&rdquo; means it is managed in the main UI. A controller should be in both.</p></div>
    <div class=card><h2>Network discovery</h2>
      <form method=get><input type=hidden name=discover value=1><button>Discover on network</button></form>
      {disc_html}
      <p class=muted>Adding a controller here appends it to the service config and reloads the REST API; add it
      in the <a href="/" style="color:var(--brass)">Access UI</a> too so it appears in the main UI with
      editable doors and cards.</p></div>"""
    return page("controllers", "Controllers", body)


if __name__ == "__main__":
    port = int(os.environ.get("PORTCULLIS_OPS_PORT", "8083"))
    prefix = os.environ.get("PORTCULLIS_OPS_PREFIX", "")   # e.g. /ops when reverse-proxied
    if prefix:
        from werkzeug.middleware.dispatcher import DispatcherMiddleware
        from werkzeug.serving import run_simple
        application = DispatcherMiddleware(Flask("_root"), {prefix: app})
        run_simple("127.0.0.1", port, application)
    else:
        app.run(host="127.0.0.1", port=port)
