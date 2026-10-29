#!/usr/bin/env bash
#
# install.sh — portable installer for the web-based door access controller stack.
#
# Stands up the uhppoted suite (https://github.com/uhppoted) on any Ubuntu host so it can
# drive the 4-door UHPPOTE panel over the network:
#   uhppoted-httpd   browser admin UI  (cardholders, doors, groups, access, live events)
#   uhppoted-rest    REST API          (drives JARVIS "open door")
#   uhppoted-mqtt    MQTT bridge        (Home Assistant door entities)  [only if --mqtt-broker given]
#   uhppote-cli      command-line tool  (scripting + card migration)
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
log "writing $ETC_DIR/uhppoted.conf ..."
cat > "$ETC_DIR/uhppoted.conf" <<EOF
# uhppoted configuration — generated by install.sh $(date -u +%FT%TZ)
# Door access control for the 4-door UHPPOTE panel.

# SYSTEM
bind.address = 0.0.0.0:0
broadcast.address = $BROADCAST
listen.address = $LISTEN

# REST
rest.http.enabled = false
rest.https.enabled = false
rest.http.port = 8080
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

# ─── 7. firewall (if ufw is active) ──────────────────────────────────────────
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  log "opening firewall ports..."
  ufw allow "$HTTP_PORT/tcp"  >/dev/null 2>&1 || true
  ufw allow "${LISTEN##*:}/udp" >/dev/null 2>&1 || true
fi

# ─── 8. enable + start ───────────────────────────────────────────────────────
svcs="uhppoted-httpd uhppoted-rest"
[ -n "$MQTT_BROKER" ] && svcs="$svcs uhppoted-mqtt"
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

Next steps:
  * First web login: create the admin user per uhppoted-httpd docs (users DB at $VAR_DIR/httpd/db).
  * Put the web UI behind your reverse proxy / restrict to LAN before exposing it.
  * Card migration: scripts/migrate-cards.sh (reads the old iCCard.mdb).

Uninstall with: sudo ./uninstall.sh   (add --purge to also remove config + data + user)
EOF
