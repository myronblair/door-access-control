#!/usr/bin/env python3
"""Portcullis Ops — Phase-B admin web app.

Pages to view events and run the attendance / patrol / meeting modules, plus settings pages to
configure each (patrol checkpoints & routes, meetings, cameras) without code changes. Reads the
event history the collector maintains in SQLite and keeps its own config in the same DB.

Runs on :8083 (put behind the Cloudflare tunnel / Access like the main UI). Flask app.
Copyright (c) 2026 TomTom Enterprises.
"""
import os, sqlite3, datetime
from flask import Flask, request, redirect, url_for, render_template_string

DB  = os.environ.get("PORTCULLIS_DB", "/var/lib/portcullis/portcullis.db")
app = Flask(__name__)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS events(idx INTEGER PRIMARY KEY, ts TEXT, card TEXT, door INTEGER,
        granted INTEGER, event_type INTEGER, raw TEXT, collected_at TEXT);
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS patrol_points(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, door INTEGER);
    CREATE TABLE IF NOT EXISTS patrol_routes(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
        doors TEXT, win_start TEXT, win_end TEXT);
    CREATE TABLE IF NOT EXISTS meetings(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, door INTEGER,
        start_ts TEXT, end_ts TEXT);
    CREATE TABLE IF NOT EXISTS cameras(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, ip TEXT,
        username TEXT, door INTEGER, rtsp TEXT);
    """)
    return c


def setting(c, k, default=""):
    r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def door_name(c, n):
    return setting(c, f"door{n}", f"Door {n}") or f"Door {n}"


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
.muted{color:var(--muted)}
</style></head><body>
<header><b>&#9113; PORTCULLIS OPS</b><span class=muted>access events &middot; attendance &middot; patrol &middot; meetings</span>
<a href="/" style="margin-left:auto;color:var(--muted);text-decoration:none">&#8592; Access UI</a></header>
<nav>
{% for p,l in pages %}<a href="{{url_for(p)}}" class="{{'on' if active==p else ''}}">{{l}}</a>{% endfor %}
</nav><main>{{ body|safe }}</main></body></html>"""

PAGES = [("dashboard","Dashboard"),("events","Events"),("attendance","Attendance"),
         ("patrol","Patrol"),("meetings","Meetings"),("cameras","Cameras"),("settings","Settings")]


def page(active, title, body):
    return render_template_string(BASE, pages=PAGES, active=active, title=title, body=body)


@app.route("/")
def dashboard():
    c = db()
    total = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
    cards = c.execute("SELECT COUNT(DISTINCT card) n FROM events").fetchone()["n"]
    last  = c.execute("SELECT MAX(ts) t FROM events").fetchone()["t"] or "—"
    recent = c.execute("SELECT * FROM events ORDER BY idx DESC LIMIT 10").fetchall()
    rows = "".join(f"<tr><td>{e['ts']}</td><td>{e['card']}</td><td>{door_name(c,e['door'])}</td>"
                   f"<td class='{'g' if e['granted'] else 'r'}'>{'granted' if e['granted'] else 'denied'}</td></tr>"
                   for e in recent) or "<tr><td colspan=4 class=muted>no events yet</td></tr>"
    body = f"""<h1>Dashboard</h1>
    <div class=tiles>
      <div class=tile><div class=n>{total}</div><div class=l>total events</div></div>
      <div class=tile><div class=n>{cards}</div><div class=l>distinct cards</div></div>
      <div class=tile><div class=n style='font-size:16px'>{last}</div><div class=l>last event</div></div>
    </div>
    <h2>Recent activity</h2><table><tr><th>Time</th><th>Card</th><th>Door</th><th>Result</th></tr>{rows}</table>"""
    return page("dashboard", "Dashboard", body)


@app.route("/events")
def events():
    c = db(); days = int(request.args.get("days", 30))
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    evs = c.execute("SELECT * FROM events WHERE ts>=? ORDER BY idx DESC LIMIT 500", (since,)).fetchall()
    rows = "".join(f"<tr><td>{e['idx']}</td><td>{e['ts']}</td><td>{e['card']}</td><td>{door_name(c,e['door'])}</td>"
                   f"<td class='{'g' if e['granted'] else 'r'}'>{'granted' if e['granted'] else 'denied'}</td>"
                   f"<td>{e['event_type']}</td></tr>" for e in evs) or "<tr><td colspan=6 class=muted>none</td></tr>"
    body = f"""<h1>Events</h1>
    <form class=row method=get><div><label>show last (days)</label><input name=days value="{days}" size=5></div><button>Filter</button></form>
    <p class=muted>{len(evs)} events (most recent 500)</p>
    <table><tr><th>#</th><th>Time</th><th>Card</th><th>Door</th><th>Result</th><th>Type</th></tr>{rows}</table>"""
    return page("events", "Events", body)


@app.route("/attendance")
def attendance():
    c = db(); days = int(request.args.get("days", 30))
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    rs = c.execute("""SELECT date(ts) d, card, MIN(ts) f, MAX(ts) l, COUNT(*) n, COALESCE(SUM(granted),0) g
                      FROM events WHERE ts>=? GROUP BY date(ts),card ORDER BY d DESC,card""", (since,)).fetchall()
    rows = "".join(f"<tr><td>{r['d']}</td><td>{r['card']}</td><td>{r['f']}</td><td>{r['l']}</td>"
                   f"<td>{r['n']}</td><td>{r['g']}</td></tr>" for r in rs) or "<tr><td colspan=6 class=muted>none</td></tr>"
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
            c.execute("INSERT INTO patrol_points(name,door) VALUES(?,?)", (f["name"], f["door"])); c.commit()
        elif f.get("add") == "route":
            c.execute("INSERT INTO patrol_routes(name,doors,win_start,win_end) VALUES(?,?,?,?)",
                      (f["name"], f["doors"], f["win_start"], f["win_end"])); c.commit()
        elif f.get("del_point"):
            c.execute("DELETE FROM patrol_points WHERE id=?", (f["del_point"],)); c.commit()
        elif f.get("del_route"):
            c.execute("DELETE FROM patrol_routes WHERE id=?", (f["del_route"],)); c.commit()
        return redirect(url_for("patrol"))
    pts = c.execute("SELECT * FROM patrol_points ORDER BY name").fetchall()
    rts = c.execute("SELECT * FROM patrol_routes ORDER BY name").fetchall()
    dsel = "".join(f"<option value={n}>{door_name(c,n)}</option>" for n in (1, 2, 3, 4))
    prows = "".join(f"<tr><td>{p['name']}</td><td>{door_name(c,p['door'])}</td>"
                    f"<td><form method=post style=display:inline><input type=hidden name=del_point value={p['id']}>"
                    f"<button class=del>delete</button></form></td></tr>" for p in pts) or "<tr><td colspan=3 class=muted>no checkpoints</td></tr>"
    # route status today
    today = datetime.date.today().isoformat()
    rrows = ""
    for r in rts:
        doors = [d for d in (r["doors"] or "").split(",") if d.strip()]
        hits = []
        for d in doors:
            n = c.execute("SELECT COUNT(*) n FROM events WHERE door=? AND date(ts)=? AND time(ts)>=? AND time(ts)<=?",
                          (d.strip(), today, r["win_start"] or "00:00", r["win_end"] or "23:59")).fetchone()["n"]
            hits.append(f"<span class='{'g' if n else 'r'}'>{door_name(c,int(d))}{'✓' if n else '✗'}</span>")
        rrows += (f"<tr><td>{r['name']}</td><td>{r['win_start']}–{r['win_end']}</td><td>{' '.join(hits)}</td>"
                  f"<td><form method=post style=display:inline><input type=hidden name=del_route value={r['id']}>"
                  f"<button class=del>delete</button></form></td></tr>")
    rrows = rrows or "<tr><td colspan=4 class=muted>no routes</td></tr>"
    body = f"""<h1>Patrol</h1>
    <div class=card><h2>Checkpoints</h2><table><tr><th>Name</th><th>Reader/Door</th><th></th></tr>{prows}</table>
      <form class=row method=post><input type=hidden name=add value=point>
        <div><label>name</label><input name=name required></div>
        <div><label>door</label><select name=door>{dsel}</select></div><button>Add checkpoint</button></form></div>
    <div class=card><h2>Routes (status for today)</h2><table><tr><th>Route</th><th>Window</th><th>Checkpoints hit</th><th></th></tr>{rrows}</table>
      <form class=row method=post><input type=hidden name=add value=route>
        <div><label>name</label><input name=name required></div>
        <div><label>doors (csv e.g. 1,2)</label><input name=doors placeholder="1,2"></div>
        <div><label>from</label><input name=win_start placeholder="22:00" size=6></div>
        <div><label>to</label><input name=win_end placeholder="06:00" size=6></div><button>Add route</button></form></div>"""
    return page("patrol", "Patrol", body)


@app.route("/meetings", methods=["GET", "POST"])
def meetings():
    c = db()
    if request.method == "POST":
        f = request.form
        if f.get("del"):
            c.execute("DELETE FROM meetings WHERE id=?", (f["del"],))
        else:
            c.execute("INSERT INTO meetings(name,door,start_ts,end_ts) VALUES(?,?,?,?)",
                      (f["name"], f["door"], f["start_ts"], f["end_ts"]))
        c.commit(); return redirect(url_for("meetings"))
    ms = c.execute("SELECT * FROM meetings ORDER BY start_ts DESC").fetchall()
    dsel = "".join(f"<option value={n}>{door_name(c,n)}</option>" for n in (1, 2, 3, 4))
    rows = ""
    for m in ms:
        att = c.execute("SELECT COUNT(DISTINCT card) n FROM events WHERE door=? AND ts>=? AND ts<=? AND granted=1",
                        (m["door"], m["start_ts"], m["end_ts"])).fetchone()["n"]
        rows += (f"<tr><td>{m['name']}</td><td>{door_name(c,m['door'])}</td><td>{m['start_ts']}</td><td>{m['end_ts']}</td>"
                 f"<td class=g>{att} signed in</td>"
                 f"<td><form method=post style=display:inline><input type=hidden name=del value={m['id']}>"
                 f"<button class=del>delete</button></form></td></tr>")
    rows = rows or "<tr><td colspan=6 class=muted>no meetings</td></tr>"
    body = f"""<h1>Meetings</h1>
    <table><tr><th>Name</th><th>Reader</th><th>Start</th><th>End</th><th>Attendance</th><th></th></tr>{rows}</table>
    <div class=card><h2>Add meeting</h2><form class=row method=post>
      <div><label>name</label><input name=name required></div>
      <div><label>reader/door</label><select name=door>{dsel}</select></div>
      <div><label>start (YYYY-MM-DD HH:MM:SS)</label><input name=start_ts size=20></div>
      <div><label>end</label><input name=end_ts size=20></div><button>Add</button></form>
      <p class=muted>Attendance = distinct cards granted at that reader within the window.</p></div>"""
    return page("meetings", "Meetings", body)


@app.route("/cameras", methods=["GET", "POST"])
def cameras():
    c = db()
    if request.method == "POST":
        f = request.form
        if f.get("del"):
            c.execute("DELETE FROM cameras WHERE id=?", (f["del"],))
        else:
            c.execute("INSERT INTO cameras(name,ip,username,door,rtsp) VALUES(?,?,?,?,?)",
                      (f["name"], f["ip"], f["username"], f["door"], f["rtsp"]))
        c.commit(); return redirect(url_for("cameras"))
    cams = c.execute("SELECT * FROM cameras ORDER BY name").fetchall()
    dsel = "".join(f"<option value={n}>{door_name(c,n)}</option>" for n in (1, 2, 3, 4))
    rows = "".join(f"<tr><td>{m['name']}</td><td>{m['ip']}</td><td>{m['username']}</td><td>{door_name(c,m['door'])}</td>"
                   f"<td class=muted>{m['rtsp'] or ''}</td>"
                   f"<td><form method=post style=display:inline><input type=hidden name=del value={m['id']}>"
                   f"<button class=del>delete</button></form></td></tr>" for m in cams) or "<tr><td colspan=6 class=muted>no cameras</td></tr>"
    body = f"""<h1>Cameras</h1>
    <table><tr><th>Name</th><th>IP</th><th>User</th><th>Door</th><th>RTSP path</th><th></th></tr>{rows}</table>
    <div class=card><h2>Add camera (Hikvision ISAPI/RTSP)</h2><form class=row method=post>
      <div><label>name</label><input name=name required></div>
      <div><label>IP</label><input name=ip placeholder="10.48.200.x"></div>
      <div><label>username</label><input name=username placeholder="admin"></div>
      <div><label>door</label><select name=door>{dsel}</select></div>
      <div><label>RTSP path (optional)</label><input name=rtsp placeholder="/Streaming/Channels/101"></div>
      <button>Add</button></form>
      <p class=muted>Stored config; snapshot-on-event uses ISAPI (/ISAPI/Streaming/channels/101/picture). Password is held on the host, not shown here.</p></div>"""
    return page("cameras", "Cameras", body)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    c = db()
    if request.method == "POST":
        for n in (1, 2, 3, 4):
            c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (f"door{n}", request.form.get(f"door{n}", "")))
        c.commit(); return redirect(url_for("settings"))
    inputs = "".join(f"<div><label>Door {n} name</label><input name=door{n} value=\"{door_name(c,n)}\"></div>" for n in (1, 2, 3, 4))
    body = f"""<h1>Settings</h1>
    <div class=card><h2>Door names</h2>
      <form class=row method=post>{inputs}<button>Save</button></form>
      <p class=muted>Names appear across Events, Attendance, Patrol and Meetings.</p></div>
    <div class=card><h2>Listener guard (anti phone-home)</h2>
      <p class=muted>Controlled by the door-control service on :8082 — <code>/guard/on</code>, <code>/guard/off</code>, <code>/guard/enforce</code> (Bearer token). Currently enforced to this host.</p></div>"""
    return page("settings", "Settings", body)


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
