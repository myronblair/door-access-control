#!/usr/bin/env python3
"""Portcullis door control + listener guard (multi-controller).

A small token-authenticated HTTP service (LAN-only, firewalled to HA + JARVIS):

  Door unlock (cardless, via `uhppote-cli open`):
    POST /open/<1-4>              unlock a door on the primary controller          [Bearer token]
    POST /open/<serial>/<1-4>     unlock a door on a specific controller           [Bearer token]

  Listener guard — defeats the OEM firmware's phone-home. It keeps EVERY configured controller's
  event listener pointed at THIS host, re-asserting any that drift and logging foreign targets.
    GET  /guard                  status (per-controller listeners + counts)        [Bearer token]
    POST /guard/on               enable (persisted; enforces all controllers now)  [Bearer token]
    POST /guard/off              disable                                           [Bearer token]
    POST /guard/enforce          one-shot: re-point every controller to self       [Bearer token]

  GET /health                    liveness (unauthenticated)

Controllers are discovered from uhppoted.conf, so adding a controller (Ops > Controllers) extends
the guard automatically. Copyright (c) 2026 TomTom Enterprises.
"""
import json, subprocess, hmac, os, threading, time, ipaddress, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import portcullis_db as pdb

CONF        = os.environ.get("PORTCULLIS_CONF", "/etc/uhppoted/uhppoted.conf")
PRIMARY     = os.environ.get("PORTCULLIS_SERIAL", "223208728")   # fallback when the config can't be read
CLI         = "/usr/local/bin/uhppote-cli"
PORT        = 8082
SELF_LISTEN = os.environ.get("PORTCULLIS_LISTENER", "10.48.200.67:60001")  # where events SHOULD go (this VM)
STATE_FILE  = "/etc/portcullis/guard.enabled"
LOG_FILE    = "/var/log/portcullis/guard.log"
CHECK_EVERY = 60
TOKEN       = open("/etc/portcullis/doorctl.token").read().strip()
LAN         = ipaddress.ip_network("10.48.200.0/24")

_state = {"last_check": None, "last_revert": None, "drift_count": 0, "last_foreign": None, "listeners": {}}
_lock  = threading.Lock()


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def log(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{now()} {msg}\n")
    except OSError:
        pass


def cli(*args, timeout=15):
    r = subprocess.run([CLI, "--config", CONF, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def serials():
    """Every controller serial declared in uhppoted.conf (falls back to the primary)."""
    return list(pdb.configured_controllers(CONF).keys()) or [PRIMARY]


def get_listener(serial):
    """The 'ip:port' a controller currently reports to, or None if unreachable."""
    rc, out = cli("get-listener", serial)
    if rc != 0 or not out:
        return None
    parts = out.split()
    return parts[1] if len(parts) >= 2 and ":" in parts[1] else None


def is_foreign(addr):
    try:
        return ipaddress.ip_address(addr.split(":")[0]) not in LAN
    except ValueError:
        return True


def enforce(serial, cur=None):
    """Point one controller's listener back to this host. Returns (changed, detail)."""
    if cur is None:
        cur = get_listener(serial)
    if cur is None:
        return False, f"{serial}: unreachable"
    if cur == SELF_LISTEN:
        return False, f"{serial}: already self"
    foreign = is_foreign(cur)
    rc, _ = cli("set-listener", serial, SELF_LISTEN)
    with _lock:
        _state["last_revert"] = now()
        _state["drift_count"] += 1
        if foreign:
            _state["last_foreign"] = f"{serial} -> {cur}"
    log(f"DRIFT ({'FOREIGN' if foreign else 'lan'}) {serial} listener was {cur} -> {SELF_LISTEN} (rc={rc})")
    return True, f"{serial}: reverted {cur} ({'FOREIGN' if foreign else 'lan'}) -> {SELF_LISTEN}"


def enforce_all():
    """Check every configured controller; re-point any that drifted. Returns {serial: detail}."""
    out = {}
    for s in serials():
        cur = get_listener(s)
        with _lock:
            _state["listeners"][s] = cur
        if cur and cur != SELF_LISTEN:
            out[s] = enforce(s, cur)[1]
        else:
            out[s] = "self" if cur == SELF_LISTEN else "unreachable"
    with _lock:
        _state["last_check"] = now()
    return out


def guard_enabled():
    try:
        return open(STATE_FILE).read().strip() == "on"
    except OSError:
        return False


def set_enabled(on):
    with open(STATE_FILE, "w") as f:
        f.write("on" if on else "off")


def guard_loop():
    first = True
    while True:
        if guard_enabled():
            try:
                if first:
                    log(f"guard enabled, enforcing {len(serials())} controller(s) -> {SELF_LISTEN}")
                enforce_all()
            except Exception as e:
                log(f"guard error: {type(e).__name__}: {e}")
        first = False
        time.sleep(CHECK_EVERY)


class H(BaseHTTPRequestHandler):
    server_version = "portcullis-doorctl"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "service": "portcullis-doorctl"})
        if self.path == "/guard":
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            with _lock:
                st = dict(_state)
            return self._send(200, {"enabled": guard_enabled(), "self": SELF_LISTEN,
                                    "controllers": serials(), **st})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        p = self.path
        if p == "/guard/on":
            set_enabled(True)
            return self._send(200, {"enabled": True, "enforced": enforce_all()})
        if p == "/guard/off":
            set_enabled(False)
            return self._send(200, {"enabled": False, "detail": "guard disabled; controllers left as-is"})
        if p == "/guard/enforce":
            return self._send(200, {"enforced": enforce_all()})
        if p.startswith("/open/"):
            rest = [x for x in p[len("/open/"):].split("/") if x]
            if len(rest) == 1:
                serial, door = serials()[0], rest[0]
            elif len(rest) == 2:
                serial, door = rest[0], rest[1]
            else:
                return self._send(400, {"error": "use /open/<door> or /open/<serial>/<door>"})
            if door not in ("1", "2", "3", "4"):
                return self._send(400, {"error": "door must be 1-4"})
            if serial not in serials():
                return self._send(400, {"error": f"unknown controller {serial}"})
            try:
                rc, out = cli("open", serial, door)
                return self._send(200 if rc == 0 else 502,
                                  {"ok": rc == 0, "controller": serial, "door": int(door), "detail": out[:200]})
            except Exception as e:
                return self._send(500, {"ok": False, "error": type(e).__name__})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    # The HTTP listener comes up immediately; the guard thread does its first enforce pass right away,
    # so a flapping controller's timeouts never delay the service binding its port.
    threading.Thread(target=guard_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
