#!/usr/bin/env python3
"""Portcullis door control + listener guard.

Two jobs, one small token-authenticated HTTP service (LAN-only, firewalled to HA + JARVIS):

  Door unlock (cardless, via `uhppote-cli open`):
    POST /open/<1-4>            unlock a door                       [Bearer token]

  Listener guard — defeats the OEM firmware's phone-home. The controller sends every access
  event to whatever listener address it's told to; the Chinese iCCard software had pointed it at
  an Alibaba Cloud server in Shenzhen. The guard keeps it pointed at THIS host ("reroute back into
  itself"). When enabled, a background thread re-asserts the listener whenever it drifts and logs
  any foreign/off-LAN target it catches.
    GET  /guard                status {enabled, listener, self, last_check, last_revert, drift_count, last_foreign}
    POST /guard/on             enable  (persisted; enforces immediately)      [Bearer token]
    POST /guard/off            disable (controller left as-is)                [Bearer token]
    POST /guard/enforce        one-shot: set listener back to self now        [Bearer token]

  GET /health                  liveness (unauthenticated)

Copyright (c) 2026 TomTom Enterprises.
"""
import json, subprocess, hmac, os, threading, time, ipaddress, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF        = os.environ.get("PORTCULLIS_CONF", "/etc/uhppoted/uhppoted.conf")
SERIAL      = os.environ.get("PORTCULLIS_SERIAL", "223208728")
CLI         = "/usr/local/bin/uhppote-cli"
PORT        = 8082
SELF_LISTEN = os.environ.get("PORTCULLIS_LISTENER", "10.48.200.67:60001")  # where events SHOULD go (this VM)
STATE_FILE  = "/etc/portcullis/guard.enabled"   # presence+content persists on/off
LOG_FILE    = "/var/log/portcullis/guard.log"
CHECK_EVERY = 60                                # seconds between guard checks
TOKEN       = open("/etc/portcullis/doorctl.token").read().strip()
LAN         = ipaddress.ip_network("10.48.200.0/24")

_state = {"last_check": None, "last_revert": None, "drift_count": 0, "last_foreign": None}
_lock  = threading.Lock()


def log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except OSError:
        pass


def cli(*args, timeout=15):
    r = subprocess.run([CLI, "--config", CONF, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def get_listener():
    """Return 'ip:port' the controller currently reports to, or None if unreachable."""
    rc, out = cli("get-listener", SERIAL)
    if rc != 0 or not out:
        return None
    # e.g. "223208728 120.78.216.117:61009 30s"  ->  "120.78.216.117:61009"
    parts = out.split()
    return parts[1] if len(parts) >= 2 and ":" in parts[1] else None


def is_foreign(addr):
    try:
        return ipaddress.ip_address(addr.split(":")[0]) not in LAN
    except ValueError:
        return True


def enforce():
    """Set the controller's listener back to this host. Returns (changed, detail)."""
    cur = get_listener()
    if cur is None:
        return False, "controller unreachable"
    if cur == SELF_LISTEN:
        return False, "already self"
    foreign = is_foreign(cur)
    rc, out = cli("set-listener", SERIAL, SELF_LISTEN)
    with _lock:
        _state["last_revert"] = datetime.datetime.now().isoformat(timespec="seconds")
        _state["drift_count"] += 1
        if foreign:
            _state["last_foreign"] = cur
    tag = "FOREIGN" if foreign else "lan"
    log(f"DRIFT ({tag}) listener was {cur} -> reset to {SELF_LISTEN} (rc={rc})")
    return True, f"reverted {cur} ({tag}) -> {SELF_LISTEN}"


def guard_enabled():
    try:
        return open(STATE_FILE).read().strip() == "on"
    except OSError:
        return False


def set_enabled(on):
    with open(STATE_FILE, "w") as f:
        f.write("on" if on else "off")


def guard_loop():
    while True:
        if guard_enabled():
            try:
                cur = get_listener()
                with _lock:
                    _state["last_check"] = datetime.datetime.now().isoformat(timespec="seconds")
                if cur and cur != SELF_LISTEN:
                    enforce()
            except Exception as e:
                log(f"guard error: {type(e).__name__}: {e}")
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
        got, want = self.headers.get("Authorization", ""), "Bearer " + TOKEN
        return len(got) == len(want) and hmac.compare_digest(got, want)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "service": "portcullis-doorctl"})
        if self.path == "/guard":
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            with _lock:
                st = dict(_state)
            return self._send(200, {"enabled": guard_enabled(), "self": SELF_LISTEN,
                                    "listener": get_listener(), **st})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        p = self.path
        if p == "/guard/on":
            set_enabled(True)
            changed, detail = enforce()
            return self._send(200, {"enabled": True, "enforced": changed, "detail": detail})
        if p == "/guard/off":
            set_enabled(False)
            return self._send(200, {"enabled": False, "detail": "guard disabled; controller left as-is"})
        if p == "/guard/enforce":
            changed, detail = enforce()
            return self._send(200, {"enforced": changed, "detail": detail})
        if p.startswith("/open/"):
            door = p.rsplit("/", 1)[-1]
            if door not in ("1", "2", "3", "4"):
                return self._send(400, {"error": "door must be 1-4"})
            try:
                rc, out = cli("open", SERIAL, door)
                return self._send(200 if rc == 0 else 502, {"ok": rc == 0, "door": int(door), "detail": out[:200]})
            except Exception as e:
                return self._send(500, {"ok": False, "error": type(e).__name__})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if guard_enabled():
        try:
            log(f"startup: guard enabled, enforcing -> {SELF_LISTEN}")
            enforce()
        except Exception as e:
            log(f"startup enforce error: {e}")
    threading.Thread(target=guard_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
