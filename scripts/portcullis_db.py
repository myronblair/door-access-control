#!/usr/bin/env python3
"""Portcullis shared database layer — one schema, one attendance query, one camera fetch.

Used by collector.py (writes events + snapshots), ops.py (web app) and report.py (CLI), so the
tables and the report logic live in exactly one place.

Copyright (c) 2026 TomTom Enterprises.
"""
import os, re, sqlite3, subprocess, datetime, json, hashlib

DB = os.environ.get("PORTCULLIS_DB", "/var/lib/portcullis/portcullis.db")

# uhppoted-httpd keeps its own authoritative stores here; the main web UI reads door/controller/card
# names from these files, so Portcullis reads the same ones rather than keeping a divergent copy.
HTTPD_SYS = os.environ.get("PORTCULLIS_HTTPD_SYS", "/var/uhppoted/httpd/system")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(idx INTEGER PRIMARY KEY, ts TEXT, card TEXT, door INTEGER,
    granted INTEGER, event_type INTEGER, raw TEXT, collected_at TEXT, controller TEXT);
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
CREATE TABLE IF NOT EXISTS peripherals(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, controller TEXT,
    door INTEGER, name TEXT, model TEXT, ip TEXT, notes TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS ops_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, who TEXT,
    action TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_opslog_ts ON ops_log(ts);
"""

# Peripheral kinds the Devices inventory tracks. Cameras keep their own richer table (credentials +
# snapshot test); keypads are read live from the controller's door flags; the rest are manual items.
PERIPHERAL_KINDS = ("lock", "closer", "reader", "sensor", "keypad", "other")


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
        if "controller" not in [r[1] for r in con.execute("PRAGMA table_info(events)")]:
            con.execute("ALTER TABLE events ADD COLUMN controller TEXT")
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


def door_keypads(serial=None):
    """Door positions (1..4) the Access UI flags as having a keypad, for the given controller."""
    oid_keypad = {d.get("OID"): bool(d.get("keypad"))
                  for d in _httpd_json("doors.json").get("doors", []) if d.get("OID")}
    ctrls = httpd_controllers()
    chosen = next((c for c in ctrls if c["serial"] == str(serial)), None) if serial else None
    if chosen is None:
        chosen = next((c for c in ctrls if c["address"]), None) or (ctrls[0] if ctrls else {})
    doors = chosen.get("doors", {}) if chosen else {}
    return {n for n in (1, 2, 3, 4) if oid_keypad.get(doors.get(str(n), ""))}


def _door_oid_to_pos(serial):
    """For a controller, map each door-record OID to its physical position 1..4."""
    c = next((x for x in httpd_controllers() if x["serial"] == str(serial)), None)
    if not c:
        c = next((x for x in httpd_controllers() if x["address"]), None) or {}
    return {oid: int(pos) for pos, oid in (c.get("doors", {}) or {}).items() if oid}


def intended_doors(serial):
    """What the Access UI DB says each assigned door's {mode, delay} should be, by position."""
    oid_rec = {d.get("OID"): d for d in _httpd_json("doors.json").get("doors", [])}
    out = {}
    for oid, pos in _door_oid_to_pos(serial).items():
        d = oid_rec.get(oid)
        if d:
            out[pos] = {"mode": (d.get("mode") or "controlled").lower(), "delay": int(d.get("delay") or 0)}
    return out


def intended_acl(serial):
    """The ACL the Access UI DB intends for this controller: {card_number: {"from","to","doors":set(pos)}}.
    A card's doors are the union of its groups' doors (uhppoted's model), mapped to this controller."""
    groups = {g.get("OID"): (g.get("doors") or []) for g in _httpd_json("groups.json").get("groups", [])}
    oid_pos = _door_oid_to_pos(serial)
    acl = {}
    for c in _httpd_json("cards.json").get("cards", []):
        if not c.get("card"):
            continue
        pos = set()
        for goid in (c.get("groups") or []):
            for doid in groups.get(goid, []):
                if doid in oid_pos:
                    pos.add(oid_pos[doid])
        if pos:                                   # only cards that open at least one door on THIS controller
            acl[str(c["card"])] = {"from": c.get("from") or "", "to": c.get("to") or "", "doors": pos}
    return acl


def controller_acl(serial, conf=None, timeout=10):
    """The cards actually on the controller: {card_number: {"from","to","doors":set(pos)}}, or None."""
    conf = conf or os.environ.get("PORTCULLIS_CONF", "/etc/uhppoted/uhppoted.conf")
    rc, out = cli(conf, "get-cards", str(serial), timeout=timeout)
    if rc != 0:
        return None
    acl = {}
    for line in out.splitlines():
        p = line.split()
        # "22322431 2016-01-01 2099-12-31 Y Y N N 345678 -"
        if len(p) >= 7 and p[0].isdigit():
            doors = {i + 1 for i in range(4) if p[3 + i].upper() == "Y"}
            acl[p[0]] = {"from": p[1], "to": p[2], "doors": doors}
    return acl


def sync_status(serial, conf=None, timeout=6):
    """Compare a controller against the Access UI DB. Returns {reachable, time, doors, acl} where each
    facet is {ok, detail}. Fast-fails if the controller is unreachable so the page never hangs."""
    conf = conf or os.environ.get("PORTCULLIS_CONF", "/etc/uhppoted/uhppoted.conf")
    import datetime as _dt
    st = {"reachable": False, "time": {"ok": None, "detail": ""}, "doors": {"ok": None, "detail": ""},
          "acl": {"ok": None, "detail": ""}}
    rc, out = cli(conf, "get-time", str(serial), timeout=timeout)
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", out)
    if rc != 0 or not m:
        return st                                 # offline — leave everything unknown
    st["reachable"] = True
    off = (_dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") - _dt.datetime.now()).total_seconds()
    st["time"] = {"ok": abs(off) <= 90,
                  "detail": "in sync" if abs(off) <= 90 else f"off by {int(abs(off))}s ({'ahead' if off>0 else 'behind'})"}

    want, diffs = intended_doors(serial), []
    for pos, w in sorted(want.items()):
        rc1, o1 = cli(conf, "get-door-control", str(serial), str(pos), timeout=timeout)
        rc2, o2 = cli(conf, "get-door-delay", str(serial), str(pos), timeout=timeout)
        mode = (re.search(r"\(([^)]+)\)", o1).group(1).lower() if rc1 == 0 and "(" in o1 else "?")
        dm = re.search(r"\b(\d+)\b\s*$", o2)
        delay = int(dm.group(1)) if rc2 == 0 and dm else -1
        if mode != w["mode"] or delay != w["delay"]:
            diffs.append(f"door {pos}: panel {mode}/{delay if delay>=0 else '?'}s vs DB {w['mode']}/{w['delay']}s")
    st["doors"] = {"ok": not diffs, "detail": "in sync" if not diffs else "; ".join(diffs)} if want \
        else {"ok": True, "detail": "no doors assigned in DB"}

    cur, want_acl = controller_acl(serial, timeout=timeout + 4), intended_acl(serial)
    if cur is None:
        st["acl"] = {"ok": None, "detail": "could not read cards"}
    else:
        extra = sorted(set(cur) - set(want_acl))
        missing = sorted(set(want_acl) - set(cur))
        changed = [c for c in set(cur) & set(want_acl)
                   if cur[c]["doors"] != want_acl[c]["doors"] or cur[c]["from"] != want_acl[c]["from"]
                   or cur[c]["to"] != want_acl[c]["to"]]
        n = len(extra) + len(missing) + len(changed)
        bits = []
        if extra:   bits.append(f"{len(extra)} on panel not in DB")
        if missing: bits.append(f"{len(missing)} in DB not on panel")
        if changed: bits.append(f"{len(changed)} differ")
        st["acl"] = {"ok": n == 0, "detail": "in sync" if n == 0 else "; ".join(bits),
                     "extra": extra, "missing": missing, "changed": changed}
    return st


def _write_store(name, obj):
    """Atomically rewrite one uhppoted-httpd system store, owner-only. Caller reloads httpd after."""
    path = os.path.join(HTTPD_SYS, name)
    tmp = path + ".portcullis.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _next_oid(items, prefix):
    mx = 0
    for it in items:
        oid = it.get("OID", "")
        if oid.startswith(prefix + "."):
            try:
                mx = max(mx, int(oid.rsplit(".", 1)[1]))
            except ValueError:
                pass
    return f"{prefix}.{mx + 1}"


def adopt_from_controller(serial, conf=None, dry_run=True):
    """Reverse sync: read the panel live and overwrite the Access UI DB's controller-derived fields
    (door mode/delay, and the full card list incl. validity + per-door access) so the DB mirrors the
    panel. Break-glass recovery for when the panel is the source of truth. Returns a change summary;
    with dry_run=False it writes the stores (caller must reload httpd)."""
    conf = conf or os.environ.get("PORTCULLIS_CONF", "/etc/uhppoted/uhppoted.conf")
    cur = controller_acl(serial, conf)
    if cur is None:
        return {"error": "controller unreachable"}
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    doors = _httpd_json("doors.json").get("doors", [])
    groups = _httpd_json("groups.json").get("groups", [])
    cards = _httpd_json("cards.json").get("cards", [])
    oid_pos = _door_oid_to_pos(serial)
    pos_oid = {p: o for o, p in oid_pos.items()}
    door_name = {d.get("OID"): (d.get("name") or "").strip() for d in doors}
    by_oid = {d.get("OID"): d for d in doors}

    door_changes = []
    for pos, doid in sorted(pos_oid.items()):
        rc1, o1 = cli(conf, "get-door-control", str(serial), str(pos))
        rc2, o2 = cli(conf, "get-door-delay", str(serial), str(pos))
        if rc1 != 0 or rc2 != 0:
            continue
        mode = re.search(r"\(([^)]+)\)", o1).group(1).lower() if "(" in o1 else ""
        dm = re.search(r"(\d+)\s*$", o2)
        delay = int(dm.group(1)) if dm else None
        rec = by_oid.get(doid)
        if rec and mode and delay is not None and (rec.get("mode") != mode or int(rec.get("delay") or 0) != delay):
            door_changes.append(f"door {pos}: {rec.get('mode')}/{rec.get('delay')}s -> {mode}/{delay}s")
            if not dry_run:
                rec["mode"], rec["delay"], rec["modified"] = mode, delay, now

    def group_for_door(doid):
        g = next((x for x in groups if x.get("doors") == [doid]), None)
        if g:
            return g["OID"]
        if dry_run:
            return f"(new:{door_name.get(doid, doid)})"
        oid = _next_oid(groups, "0.5")
        groups.append({"OID": oid, "name": door_name.get(doid) or f"Door {doid}", "doors": [doid],
                       "created": now, "modified": ""})
        return oid

    by_num = {str(c.get("card")): c for c in cards}
    adds, updates, removes = [], [], []
    for num, info in cur.items():
        gids = [group_for_door(pos_oid[p]) for p in sorted(info["doors"]) if p in pos_oid]
        existing = by_num.get(num)
        if existing:
            updates.append(num)
            if not dry_run:
                existing.update(**{"from": info["from"]}, to=info["to"], groups=gids, modified=now)
                if not existing.get("name"):
                    existing["name"] = f"Card {num}"
        else:
            adds.append(num)
            if not dry_run:
                cards.append({"OID": _next_oid(cards, "0.4"), "name": f"Card {num}", "card": int(num),
                              "from": info["from"], "to": info["to"], "groups": gids,
                              "created": now, "modified": ""})
    for c in list(cards):
        if str(c.get("card")) not in cur:
            removes.append(str(c.get("card")))
            if not dry_run:
                cards.remove(c)

    if not dry_run:
        _write_store("doors.json", {"doors": doors})
        _write_store("groups.json", {"groups": groups})
        _write_store("cards.json", {"cards": cards})
    return {"dry_run": dry_run, "door_changes": door_changes,
            "cards_add": adds, "cards_update": updates, "cards_remove": removes}


def _utcnow():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Access UI store CRUD (caller reloads httpd after a write) ──────────────────────────────────────
def groups_all():   return _httpd_json("groups.json").get("groups", [])
def doors_all():    return _httpd_json("doors.json").get("doors", [])
def users_all():    return _httpd_json("users.json").get("users", [])


def save_group(oid, name, door_oids):
    """Create (oid falsy) or update a group. Returns the OID."""
    gs = groups_all()
    g = next((x for x in gs if x.get("OID") == oid), None) if oid else None
    if g:
        g.update(name=name, doors=list(door_oids), modified=_utcnow())
    else:
        oid = _next_oid(gs, "0.5")
        gs.append({"OID": oid, "name": name, "doors": list(door_oids), "created": _utcnow(), "modified": ""})
    _write_store("groups.json", {"groups": gs})
    return oid


def delete_group(oid):
    _write_store("groups.json", {"groups": [g for g in groups_all() if g.get("OID") != oid]})


def _hash_pw(pwd):
    """uhppoted-httpd scheme: salt = 16 random bytes; password = hex(sha256(salt || utf8(pwd)))."""
    salt = os.urandom(16)
    return salt.hex(), hashlib.sha256(salt + (pwd or "").encode("utf-8")).hexdigest()


def save_user(oid, uid, role, password=None):
    """Create or update an Access-UI login. Only sets the password when one is supplied."""
    us = users_all()
    u = next((x for x in us if x.get("OID") == oid), None) if oid else None
    if u:
        u.update(uid=uid, role=role, modified=_utcnow())
        if password:
            u["salt"], u["password"] = _hash_pw(password)
    else:
        salt, pw = _hash_pw(password or "")
        us.append({"OID": _next_oid(us, "0.8"), "name": uid, "uid": uid, "role": role,
                   "salt": salt, "password": pw, "otp": "", "created": _utcnow(), "modified": ""})
    _write_store("users.json", {"users": us})


def delete_user(oid):
    _write_store("users.json", {"users": [u for u in users_all() if u.get("OID") != oid]})


def ensure_door_oid(serial, pos, name=None):
    """Return the door-record OID for (serial, position), creating + linking one if absent."""
    ctrls = _httpd_json("controllers.json").get("controllers", [])
    cc = next((c for c in ctrls if str(c.get("device-id")) == str(serial)), None)
    if not cc:
        return None
    oid = (cc.get("doors") or {}).get(str(pos), "")
    if oid:
        return oid
    ds = doors_all()
    oid = _next_oid(ds, "0.3")
    ds.append({"OID": oid, "name": name or f"Door {pos}", "delay": 5, "mode": "controlled",
               "created": _utcnow(), "modified": ""})
    _write_store("doors.json", {"doors": ds})
    cc.setdefault("doors", {})[str(pos)] = oid
    cc["modified"] = _utcnow()
    _write_store("controllers.json", {"controllers": ctrls})
    return oid


def save_door(oid, name, keypad):
    """Update a door record's name + keypad flag (mode/delay go to the controller, not here)."""
    ds = doors_all()
    d = next((x for x in ds if x.get("OID") == oid), None)
    if not d:
        return False
    d.update(name=name, keypad=bool(keypad), modified=_utcnow())
    _write_store("doors.json", {"doors": ds})
    return True


def _group_for_door_oid(doid, door_name=""):
    """OID of a single-door group for this door, creating one if none exists (re-reads each call)."""
    gs = groups_all()
    g = next((x for x in gs if x.get("doors") == [doid]), None)
    if g:
        return g["OID"]
    oid = _next_oid(gs, "0.5")
    gs.append({"OID": oid, "name": door_name or f"Door {doid}", "doors": [doid],
               "created": _utcnow(), "modified": ""})
    _write_store("groups.json", {"groups": gs})
    return oid


def save_card_record(serial, num, frm, to, positions, name):
    """Upsert a card's name/validity/group-doors in cards.json so the Access UI matches what Ops just
    wrote to the controller. positions = list of door numbers (1-4) granted on `serial`."""
    pos_oid = {p: o for o, p in _door_oid_to_pos(serial).items()}
    dn = {d["OID"]: (d.get("name") or "") for d in doors_all()}
    gids = [_group_for_door_oid(pos_oid[int(p)], dn.get(pos_oid[int(p)], "")) for p in positions if int(p) in pos_oid]
    cards = _httpd_json("cards.json").get("cards", [])
    ex = next((c for c in cards if str(c.get("card")) == str(num)), None)
    if ex:
        ex.update(**{"from": frm}, to=to, groups=gids, modified=_utcnow())
        if name:
            ex["name"] = name
    else:
        cards.append({"OID": _next_oid(cards, "0.4"), "name": name or f"Card {num}", "card": int(num),
                      "from": frm, "to": to, "groups": gids, "created": _utcnow(), "modified": ""})
    _write_store("cards.json", {"cards": cards})


def delete_card_record(num):
    cards = [c for c in _httpd_json("cards.json").get("cards", []) if str(c.get("card")) != str(num)]
    _write_store("cards.json", {"cards": cards})


def discover_onvif(timeout=3):
    """Broadcast an ONVIF WS-Discovery probe and return discovered camera endpoints [{ip, xaddr}].
    Pure-stdlib UDP multicast — most IP cameras (Hikvision/Dahua/ONVIF) answer with their address."""
    import socket, uuid, time as _t
    probe = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header>'
        f'<w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>'
        '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
        '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>'
        '</e:Body></e:Envelope>')
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    found = {}
    try:
        s.sendto(probe.encode(), ("239.255.255.250", 3702))
        end = _t.time() + timeout
        while _t.time() < end:
            try:
                data, addr = s.recvfrom(65535)
            except (socket.timeout, OSError):
                break
            text = data.decode("utf-8", "replace")
            xa = re.search(r"XAddrs>([^<]+)<", text)
            urls = xa.group(1).split() if xa else []
            ip = addr[0]
            for u in urls:
                m = re.search(r"https?://([\d.]+)", u)
                if m:
                    ip = m.group(1)
                    break
            found.setdefault(ip, {"ip": ip, "xaddr": urls[0] if urls else "", "src": addr[0]})
    except OSError:
        pass
    finally:
        s.close()
    return list(found.values())


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
