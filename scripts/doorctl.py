#!/usr/bin/env python3
"""Portcullis door-control shim — a tiny token-authenticated HTTP endpoint that both Home Assistant
and JARVIS call to unlock a door, so neither needs the uhppoted protocol or a card. Cardless remote
unlock via `uhppote-cli open`, works on any of the 4 doors. LAN-only (firewalled to HA + JARVIS) and
bearer-token protected.

  GET  /health            -> {"ok": true}                 (unauthenticated liveness)
  POST /open/<1-4>        -> runs `uhppote-cli open`       (requires  Authorization: Bearer <token>)

Token is read from /etc/portcullis/doorctl.token. Controller serial + config are fixed below.
Copyright (c) 2026 TomTom Enterprises.
"""
import json, subprocess, hmac, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF   = "/etc/uhppoted/uhppoted.conf"
SERIAL = "223208728"
CLI    = "/usr/local/bin/uhppote-cli"
PORT   = 8082
TOKEN  = open("/etc/portcullis/doorctl.token").read().strip()

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
        got = self.headers.get("Authorization", "")
        want = "Bearer " + TOKEN
        return len(got) == len(want) and hmac.compare_digest(got, want)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "service": "portcullis-doorctl"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/open/"):
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        door = self.path.rsplit("/", 1)[-1]
        if door not in ("1", "2", "3", "4"):
            return self._send(400, {"error": "door must be 1-4"})
        try:
            r = subprocess.run([CLI, "--config", CONF, "open", SERIAL, door],
                               capture_output=True, text=True, timeout=15)
            ok = r.returncode == 0
            self._send(200 if ok else 502,
                       {"ok": ok, "door": int(door),
                        "detail": (r.stdout + r.stderr).strip()[:200]})
        except Exception as e:
            self._send(500, {"ok": False, "error": type(e).__name__})

    def log_message(self, *a):  # quieter logs
        pass

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
