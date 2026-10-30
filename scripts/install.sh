#!/usr/bin/env bash
#
# install.sh — portable installer for the web-based door access controller stack.
#
# Stands up the uhppoted suite (https://github.com/uhppoted) on any Ubuntu host so it can
# drive the 4-door UHPPOTE panel over the network:
#   uhppoted-httpd      browser admin UI (cardholders, doors, groups, access, live events)
#   uhppoted-rest       REST API         (reads/writes; firewalled to the integration hosts)
#   uhppoted-mqtt       MQTT bridge      (Home Assistant door entities) [only if --mqtt-broker given]
#   uhppote-cli         command-line tool
#   portcullis-doorctl  token-auth door-unlock shim + LISTENER GUARD (keeps the controller's event
#                       listener pointed at THIS host, defeating the OEM firmware's phone-home)
# It also applies the Portcullis branding (logo + theme) to the web UI if ../branding is present.
#
# Builds the binaries from source with a pinned Go toolchain, creates a dedicated service user,
# writes /etc/uhppoted/uhppoted.conf for your controller, installs systemd units and starts them.
# Safe to re-run (idempotent). Pair with uninstall.sh to remove it.
#
# Usage (run as root):
#   sudo ./install.sh [options]
# Options (all have sane defaults for the Blair panel):
#   --serial N            controller serial number        (default 223208728)
#   --controller IP       controller IP                   (default 10.48.200.61)
#   --doors "A,B,C,D"     names for doors 1..4            (default "Door1,Door2,Door3,Door4")
#   --http-port PORT      web UI http port (TLS on proxy)  (default 8080)
#   --broadcast IP:PORT   UDP broadcast for discovery      (default 10.48.200.255:60000)
#   --listen IP:PORT      UDP listen for controller events (default 0.0.0.0:60001)
#   --mqtt-broker URL     enable MQTT bridge to this broker e.g. tcp://10.48.200.97:1883
#   --integrations IPs    comma-list allowed to reach REST+shim (default JARVIS,HA LAN IPs)
#   --no-guard            do NOT enable the listener guard (left disabled)
#   --go-version X.Y.Z    Go toolchain to fetch            (default 1.23.3)
#   --no-start            install but do not start services
#
# Copyright (c) 2026 TomTom Enterprises.

set -euo pipefail

# ─── defaults ────────────────────────────────────────────────────────────────
SERIAL="223208728"
CONTROLLER="10.48.200.61"
DOORS="Door1,Door2,Door3,Door4"
HTTP_PORT="8080"
BROADCAST="10.48.200.255:60000"
LISTEN="0.0.0.0:60001"
MQTT_BROKER=""
GO_VERSION="1.23.3"
START=1
GUARD=1                                       # listener guard ON by default (anti phone-home)
INTEGRATIONS="10.48.200.211,10.48.200.97"     # hosts allowed to reach REST + door shim (JARVIS, HA)
REST_PORT="8081"
SHIM_PORT="8082"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORTCULLIS_DIR="/opt/portcullis"
PORTCULLIS_ETC="/etc/portcullis"
PORTCULLIS_LOG="/var/log/portcullis"

UHPPOTED_USER="uhppoted"
SRC_DIR="/opt/uhppoted/src"
ETC_DIR="/etc/uhppoted"
VAR_DIR="/var/uhppoted"
LOG_DIR="/var/log/uhppoted"
BIN_DIR="/usr/local/bin"
GO_ROOT="/usr/local/go"
COMPONENTS="uhppote-cli uhppoted-rest uhppoted-httpd uhppoted-mqtt"

log()  { printf '\e[36m[install]\e[0m %s\n' "$*"; }
warn() { printf '\e[33m[warn]\e[0m %s\n'  "$*"; }
die()  { printf '\e[31m[error]\e[0m %s\n' "$*" >&2; exit 1; }

# ─── args ────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --serial)      SERIAL="$2"; shift 2;;
    --controller)  CONTROLLER="$2"; shift 2;;
    --doors)       DOORS="$2"; shift 2;;
    --http-port)   HTTP_PORT="$2"; shift 2;;
    --broadcast)   BROADCAST="$2"; shift 2;;
    --listen)      LISTEN="$2"; shift 2;;
    --mqtt-broker) MQTT_BROKER="$2"; shift 2;;
    --integrations) INTEGRATIONS="$2"; shift 2;;
    --no-guard)    GUARD=0; shift;;
    --go-version)  GO_VERSION="$2"; shift 2;;
    --no-start)    START=0; shift;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0;;
    *)             die "unknown option: $1";;
  esac
done

[ "$(id -u)" = "0" ] || die "run as root (sudo)."
command -v apt-get >/dev/null 2>&1 || die "this installer targets Ubuntu/Debian (apt)."

IFS=',' read -r D1 D2 D3 D4 <<EOF
$DOORS
EOF
D1="${D1:-Door1}"; D2="${D2:-Door2}"; D3="${D3:-Door3}"; D4="${D4:-Door4}"

log "controller $SERIAL @ $CONTROLLER  doors: $D1 / $D2 / $D3 / $D4"
[ -n "$MQTT_BROKER" ] && log "MQTT bridge -> $MQTT_BROKER" || log "MQTT bridge: disabled"

# ─── 1. OS packages ──────────────────────────────────────────────────────────
log "installing build dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git make curl ca-certificates mdbtools jq >/dev/null

# ─── 2. Go toolchain (pinned) ────────────────────────────────────────────────
need_go=1
if [ -x "$GO_ROOT/bin/go" ] && "$GO_ROOT/bin/go" version 2>/dev/null | grep -q "go$GO_VERSION"; then
  need_go=0
fi
if [ "$need_go" = "1" ]; then
  arch="$(dpkg --print-architecture)"   # amd64 | arm64
  tarball="go${GO_VERSION}.linux-${arch}.tar.gz"
  log "installing Go ${GO_VERSION} (${arch})..."
  curl -fsSL "https://go.dev/dl/${tarball}" -o "/tmp/${tarball}"
  rm -rf "$GO_ROOT"
  tar -C /usr/local -xzf "/tmp/${tarball}"
  rm -f "/tmp/${tarball}"
fi
export PATH="$GO_ROOT/bin:$PATH"
export GOPATH=/opt/uhppoted/go GOCACHE=/opt/uhppoted/.cache
go version

# ─── 3. service user + dirs ──────────────────────────────────────────────────
if ! id "$UHPPOTED_USER" >/dev/null 2>&1; then
  log "creating service user '$UHPPOTED_USER'..."
  useradd --system --home-dir "$VAR_DIR" --shell /usr/sbin/nologin "$UHPPOTED_USER"
fi
mkdir -p "$SRC_DIR" "$ETC_DIR" "$VAR_DIR" "$LOG_DIR"

# ─── 4. build + install the uhppoted binaries ────────────────────────────────
for c in $COMPONENTS; do
  if [ -d "$SRC_DIR/$c/.git" ]; then
    log "updating $c..."; git -C "$SRC_DIR/$c" pull --quiet --ff-only || warn "$c: pull skipped"
  else
    log "cloning $c..."; git clone --depth 1 --quiet "https://github.com/uhppoted/$c.git" "$SRC_DIR/$c"
  fi
  log "building $c..."
  ( cd "$SRC_DIR/$c" && GOBIN="$BIN_DIR" go install -trimpath ./... )
done
# uhppoted-httpd needs a scaffold beyond the binary: web assets + grule rule files + seed data
# (system/*.json) + auth.json. Lay down the distribution's example set (docker/compose). The admin
# user is created on first web login via the setup page.
install -d "$VAR_DIR/httpd/html" "$VAR_DIR/httpd/system" "$ETC_DIR/httpd/grules"
[ -d "$SRC_DIR/uhppoted-httpd/httpd/html" ] && cp -r "$SRC_DIR/uhppoted-httpd/httpd/html/." "$VAR_DIR/httpd/html/" 2>/dev/null || true
HS="$SRC_DIR/uhppoted-httpd/docker/compose"
cp "$HS"/system/*.json "$VAR_DIR/httpd/system/" 2>/dev/null || true
cp "$HS"/grules/*.grl  "$ETC_DIR/httpd/grules/" 2>/dev/null || true
cp "$HS/acl.grl"   "$ETC_DIR/httpd/" 2>/dev/null || true
cp "$HS/auth.json" "$ETC_DIR/httpd/" 2>/dev/null || true
log "installed: $(ls $BIN_DIR | grep -E 'uhppot' | tr '\n' ' ')"

# ─── 5. configuration ────────────────────────────────────────────────────────
REST_ENABLED=$([ -n "$INTEGRATIONS" ] && echo true || echo false)   # REST on only if integrations use it
log "writing $ETC_DIR/uhppoted.conf ..."
cat > "$ETC_DIR/uhppoted.conf" <<EOF
# uhppoted configuration — generated by install.sh $(date -u +%FT%TZ)
# Door access control for the 4-door UHPPOTE panel.

# SYSTEM
bind.address = 0.0.0.0:0
broadcast.address = $BROADCAST
listen.address = $LISTEN

# REST (LAN-only; firewalled to the integration hosts)
rest.http.enabled = $REST_ENABLED
rest.https.enabled = false
rest.http.port = $REST_PORT
rest.https.port = 8443
rest.tls.ca = $ETC_DIR/rest/ca.cert
rest.cors.enabled = false

# MQTT
mqtt.broker = ${MQTT_BROKER:-tcp://127.0.0.1:1883}
mqtt.topic.root = uhppoted

# HTTPD (serves plain HTTP; put TLS on a reverse proxy in front)
httpd.html = $VAR_DIR/httpd/html
httpd.http.enabled = true
httpd.http.port = $HTTP_PORT
httpd.https.enabled = false
httpd.security.auth = basic
httpd.security.local.db = $ETC_DIR/httpd/auth.json
httpd.db.rules.acl = $ETC_DIR/httpd/acl.grl
httpd.db.rules.interfaces = $ETC_DIR/httpd/grules/interfaces.grl
httpd.db.rules.controllers = $ETC_DIR/httpd/grules/controllers.grl
httpd.db.rules.cards = $ETC_DIR/httpd/grules/cards.grl
httpd.db.rules.doors = $ETC_DIR/httpd/grules/doors.grl
httpd.db.rules.groups = $ETC_DIR/httpd/grules/groups.grl
httpd.db.rules.events = $ETC_DIR/httpd/grules/events.grl
httpd.db.rules.logs = $ETC_DIR/httpd/grules/logs.grl
httpd.db.rules.users = $ETC_DIR/httpd/grules/users.grl

# DEVICES
UT0311-L0x.$SERIAL.address = $CONTROLLER:60000
UT0311-L0x.$SERIAL.door.1 = $D1
UT0311-L0x.$SERIAL.door.2 = $D2
UT0311-L0x.$SERIAL.door.3 = $D3
UT0311-L0x.$SERIAL.door.4 = $D4
EOF
chown -R "$UHPPOTED_USER:$UHPPOTED_USER" "$VAR_DIR" "$LOG_DIR" "$ETC_DIR"
chmod 644 "$ETC_DIR/uhppoted.conf"   # no secrets (device address/door names); CLI needs to read it

# ─── 6. systemd units ────────────────────────────────────────────────────────
mk_unit() {  # name  binary  args  description
  cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$4
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$UHPPOTED_USER
Group=$UHPPOTED_USER
ExecStart=$BIN_DIR/$2 $3
Restart=on-failure
RestartSec=5
WorkingDirectory=$VAR_DIR
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$VAR_DIR $LOG_DIR
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
}
log "installing systemd units..."
mk_unit uhppoted-httpd uhppoted-httpd "--config $ETC_DIR/uhppoted.conf" "uhppoted door access — web UI"
mk_unit uhppoted-rest  uhppoted-rest  "--config $ETC_DIR/uhppoted.conf" "uhppoted door access — REST API"
[ -n "$MQTT_BROKER" ] && mk_unit uhppoted-mqtt uhppoted-mqtt "--config $ETC_DIR/uhppoted.conf" "uhppoted door access — MQTT bridge"
systemctl daemon-reload

# ─── 7. Portcullis branding (logo + theme) ───────────────────────────────────
if [ -x "$HERE/../branding/apply-branding.sh" ]; then
  log "applying Portcullis branding..."
  "$HERE/../branding/apply-branding.sh" "$VAR_DIR/httpd/html" >/dev/null 2>&1 || warn "branding step skipped"
fi

# ─── 8. door-control shim + listener guard ───────────────────────────────────
if [ -f "$HERE/doorctl.py" ]; then
  log "installing portcullis-doorctl (door shim + listener guard, guard=$([ "$GUARD" = 1 ] && echo on || echo off))..."
  command -v python3 >/dev/null 2>&1 || apt-get install -y -qq python3 >/dev/null
  install -d -m755 "$PORTCULLIS_DIR"; install -d -m750 "$PORTCULLIS_ETC"; install -d -m750 "$PORTCULLIS_LOG"
  install -m755 "$HERE/doorctl.py" "$PORTCULLIS_DIR/doorctl.py"
  [ -f "$PORTCULLIS_ETC/doorctl.token" ] || openssl rand -hex 24 > "$PORTCULLIS_ETC/doorctl.token"
  chmod 600 "$PORTCULLIS_ETC/doorctl.token"
  printf '%s\n' "$([ "$GUARD" = 1 ] && echo on || echo off)" > "$PORTCULLIS_ETC/guard.enabled"
  chown -R "$UHPPOTED_USER:$UHPPOTED_USER" "$PORTCULLIS_DIR" "$PORTCULLIS_ETC" "$PORTCULLIS_LOG"
  SELF_LISTENER="$(hostname -I | awk '{print $1}'):${LISTEN##*:}"
  cat > /etc/systemd/system/portcullis-doorctl.service <<EOF
[Unit]
Description=Portcullis door-control shim + listener guard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$UHPPOTED_USER
Group=$UHPPOTED_USER
Environment=PORTCULLIS_SERIAL=$SERIAL
Environment=PORTCULLIS_LISTENER=$SELF_LISTENER
ExecStart=/usr/bin/python3 $PORTCULLIS_DIR/doorctl.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$PORTCULLIS_ETC $PORTCULLIS_LOG
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
fi

# ─── 8b. event collector + attendance (Phase B foundation) ───────────────────
if [ -f "$HERE/collector.py" ]; then
  log "installing portcullis-collector (access events -> SQLite)..."
  install -m755 "$HERE/collector.py" "$PORTCULLIS_DIR/collector.py"
  install -m644 "$HERE/portcullis_db.py" "$PORTCULLIS_DIR/portcullis_db.py"   # shared schema + attendance + camera fetch
  [ -f "$HERE/report.py" ] && install -m755 "$HERE/report.py" "$PORTCULLIS_DIR/report.py"
  install -d -m700 -o "$UHPPOTED_USER" -g "$UHPPOTED_USER" /var/lib/portcullis
  chown "$UHPPOTED_USER:$UHPPOTED_USER" "$PORTCULLIS_DIR"/collector.py "$PORTCULLIS_DIR"/portcullis_db.py "$PORTCULLIS_DIR"/report.py 2>/dev/null || true
  cat > /etc/systemd/system/portcullis-collector.service <<EOF
[Unit]
Description=Portcullis event collector (access events -> SQLite)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$UHPPOTED_USER
Group=$UHPPOTED_USER
Environment=PORTCULLIS_SERIAL=$SERIAL
ExecStart=/usr/bin/python3 $PORTCULLIS_DIR/collector.py
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=/var/lib/portcullis
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
fi

# ─── 8c. Ops web app (Phase B) behind nginx on the same URL (/ops) ────────────
if [ -f "$HERE/ops.py" ]; then
  log "installing Portcullis Ops web app + nginx front (web UI at /, ops at /ops)..."
  command -v nginx >/dev/null 2>&1 || apt-get install -y -qq nginx >/dev/null
  if [ ! -x "$PORTCULLIS_DIR/venv/bin/python" ]; then
    apt-get install -y -qq python3-venv >/dev/null
    python3 -m venv "$PORTCULLIS_DIR/venv"
    "$PORTCULLIS_DIR/venv/bin/pip" install -q flask >/dev/null 2>&1 || true
  fi
  "$PORTCULLIS_DIR/venv/bin/python" -c 'import flask' 2>/dev/null \
    || warn "Flask is not importable in $PORTCULLIS_DIR/venv — portcullis-ops will not start (try: $PORTCULLIS_DIR/venv/bin/pip install flask)"
  install -m755 "$HERE/ops.py" "$PORTCULLIS_DIR/ops.py"
  # privileged helpers: root-owned, invoked by Ops via tightly-restricted NOPASSWD sudo rules.
  #   portcullis-add-controller  — Controllers "Discover + Add" (appends a controller to the config)
  #   portcullis-reload-httpd    — restart uhppoted-httpd after Ops writes its JSON (adopt / door edits)
  : > /tmp/pc-sudoers
  if [ -f "$HERE/add-controller.sh" ]; then
    install -m755 -o root -g root "$HERE/add-controller.sh" /usr/local/sbin/portcullis-add-controller
    echo "$UHPPOTED_USER ALL=(root) NOPASSWD: /usr/local/sbin/portcullis-add-controller" >> /tmp/pc-sudoers
  fi
  if [ -f "$HERE/reload-httpd.sh" ]; then
    install -m755 -o root -g root "$HERE/reload-httpd.sh" /usr/local/sbin/portcullis-reload-httpd
    echo "$UHPPOTED_USER ALL=(root) NOPASSWD: /usr/local/sbin/portcullis-reload-httpd" >> /tmp/pc-sudoers
  fi
  if [ -s /tmp/pc-sudoers ]; then
    visudo -cf /tmp/pc-sudoers && install -m440 -o root -g root /tmp/pc-sudoers /etc/sudoers.d/portcullis
  fi
  rm -f /tmp/pc-sudoers
  install -m644 "$HERE/portcullis_db.py" "$PORTCULLIS_DIR/portcullis_db.py"
  install -d -m700 -o "$UHPPOTED_USER" -g "$UHPPOTED_USER" /var/lib/portcullis
  chown -R "$UHPPOTED_USER:$UHPPOTED_USER" "$PORTCULLIS_DIR"
  cat > /etc/systemd/system/portcullis-ops.service <<EOF
[Unit]
Description=Portcullis Ops (Phase B web app)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$UHPPOTED_USER
Group=$UHPPOTED_USER
Environment=PORTCULLIS_OPS_PREFIX=/ops
Environment=PORTCULLIS_OPS_PORT=8083
# Ops trusts the Cloudflare Access identity header; restrict to specific emails with PORTCULLIS_OPS_ALLOW=a@x.com,b@y.com
# (set PORTCULLIS_OPS_AUTH=off only for a LAN-only install with no Cloudflare Access in front)
Environment=PORTCULLIS_OPS_AUTH=access
ExecStart=$PORTCULLIS_DIR/venv/bin/python $PORTCULLIS_DIR/ops.py
Restart=on-failure
RestartSec=5
# Ops uses sudo for two tightly-whitelisted root helpers (add-controller, reload-httpd), so it cannot
# set NoNewPrivileges. It still runs unprivileged as $UHPPOTED_USER with the filesystem protections below.
ProtectSystem=full
ReadWritePaths=/var/lib/portcullis /var/uhppoted/httpd/system
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  # uhppoted-httpd moves to :8088 (internal); nginx fronts $HTTP_PORT (what the tunnel hits)
  if grep -q '^httpd.http.port' "$ETC_DIR/uhppoted.conf"; then
    sed -i 's/^httpd.http.port.*/httpd.http.port = 8088/' "$ETC_DIR/uhppoted.conf"
  else
    echo 'httpd.http.port = 8088' >> "$ETC_DIR/uhppoted.conf"
  fi
  grep -q '^httpd.http.port = 8088$' "$ETC_DIR/uhppoted.conf" \
    || warn "could not move uhppoted-httpd to :8088 in $ETC_DIR/uhppoted.conf — nginx and httpd will both want :$HTTP_PORT"
  cat > /etc/nginx/sites-available/portcullis <<NG
server {
  listen $HTTP_PORT default_server;
  server_name _;
  location = /ops { return 302 /ops/; }
  location /ops/ { proxy_pass http://127.0.0.1:8083; proxy_set_header Host \$host; proxy_set_header X-Forwarded-For \$remote_addr; }
  location ~* \.css\$ { proxy_pass http://127.0.0.1:8088; proxy_set_header Host \$host; add_header Cache-Control "no-cache, must-revalidate" always; }
  location / { proxy_pass http://127.0.0.1:8088; proxy_set_header Host \$host; proxy_set_header X-Forwarded-For \$remote_addr; }
}
NG
  rm -f /etc/nginx/sites-enabled/default
  ln -sf /etc/nginx/sites-available/portcullis /etc/nginx/sites-enabled/portcullis
  systemctl daemon-reload
  if nginx -t >/dev/null 2>&1; then
    systemctl restart nginx || warn "nginx failed to restart (journalctl -u nginx) — the web UI will be down until it does"
  else
    warn "nginx config test failed (nginx -t) — the web UI will be down until it is fixed"
  fi
fi

# ─── 9. firewall — lock REST + door shim to the integration hosts only ────────
if command -v ufw >/dev/null 2>&1; then
  log "configuring firewall (ufw): SSH + controller events open; REST/$REST_PORT + shim/$SHIM_PORT limited to integrations..."
  ufw allow 22/tcp >/dev/null 2>&1 || true
  ufw allow "${LISTEN##*:}/udp" >/dev/null 2>&1 || true
  IFS=',' read -ra _IH <<< "$INTEGRATIONS"
  for ip in "${_IH[@]}"; do
    [ -n "$ip" ] || continue
    ufw allow from "$ip" to any port "$REST_PORT" proto tcp >/dev/null 2>&1 || true
    ufw allow from "$ip" to any port "$SHIM_PORT" proto tcp >/dev/null 2>&1 || true
  done
  yes | ufw --force enable >/dev/null 2>&1 || true
fi

# ─── 10. enable + start ──────────────────────────────────────────────────────
svcs="uhppoted-httpd uhppoted-rest"
[ -n "$MQTT_BROKER" ] && svcs="$svcs uhppoted-mqtt"
[ -f "$HERE/doorctl.py" ] && svcs="$svcs portcullis-doorctl"
[ -f "$HERE/collector.py" ] && svcs="$svcs portcullis-collector"
[ -f "$HERE/ops.py" ] && svcs="$svcs portcullis-ops"
if [ "$START" = "1" ]; then
  log "enabling + starting services..."
  for s in $svcs; do systemctl enable --now "$s" >/dev/null 2>&1 || warn "$s failed to start (check: journalctl -u $s)"; done
else
  log "enabling services (not started, --no-start)..."
  for s in $svcs; do systemctl enable "$s" >/dev/null 2>&1 || true; done
fi

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

$(printf '\e[32m[done]\e[0m') uhppoted door access control installed.

  Web UI : http://${ip:-<this-host>}:$HTTP_PORT   (put TLS on a reverse proxy in front for remote access)
  Verify : uhppote-cli --config $ETC_DIR/uhppoted.conf get-devices   (expect serial $SERIAL)
  Status : systemctl status $svcs
  Logs   : journalctl -u uhppoted-httpd -f

  Door shim : http://${ip:-<host>}:$SHIM_PORT   (token at $PORTCULLIS_ETC/doorctl.token; LAN-only)
  Listener guard: $([ "$GUARD" = 1 ] && echo "ON — keeps the controller reporting to this host" || echo "off")

Door shim / guard control (Bearer token = $PORTCULLIS_ETC/doorctl.token):
  curl -H "Authorization: Bearer \$TOK" http://<host>:$SHIM_PORT/guard            # status
  curl -H "Authorization: Bearer \$TOK" -X POST http://<host>:$SHIM_PORT/guard/on # protect
  curl -H "Authorization: Bearer \$TOK" -X POST http://<host>:$SHIM_PORT/guard/off
  curl -H "Authorization: Bearer \$TOK" -X POST http://<host>:$SHIM_PORT/open/1   # unlock door 1

Next steps:
  * First web login: create the admin user on the setup page, then set httpd.security.no-setup=true.
  * Put the web UI behind a reverse proxy / Cloudflare Access before exposing it.

Uninstall with: sudo ./uninstall.sh   (add --purge to also remove config + data + user)
EOF
