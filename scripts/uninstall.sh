#!/usr/bin/env bash
#
# uninstall.sh — remove the web-based door access controller stack installed by install.sh.
#
# By default removes the services, binaries and build tree, but KEEPS your configuration, the
# cardholder/web database and the service user (so a re-install keeps your data).
#
# Usage (run as root):
#   sudo ./uninstall.sh            remove services + binaries + source (keep config/data/user)
#   sudo ./uninstall.sh --purge    also remove /etc/uhppoted, /var/uhppoted, logs and the user
#   sudo ./uninstall.sh --purge-go also remove the Go toolchain this installer placed in /usr/local/go
#
# Copyright (c) 2026 TomTom Enterprises.

set -euo pipefail

PURGE=0
PURGE_GO=0
UHPPOTED_USER="uhppoted"
SRC_PARENT="/opt/uhppoted"
ETC_DIR="/etc/uhppoted"
VAR_DIR="/var/uhppoted"
LOG_DIR="/var/log/uhppoted"
BIN_DIR="/usr/local/bin"
GO_ROOT="/usr/local/go"
SERVICES="uhppoted-httpd uhppoted-rest uhppoted-mqtt portcullis-doorctl portcullis-collector portcullis-ops"
BINARIES="uhppote-cli uhppoted-rest uhppoted-httpd uhppoted-mqtt"
PORTCULLIS_DIR="/opt/portcullis"
PORTCULLIS_ETC="/etc/portcullis"
PORTCULLIS_LOG="/var/log/portcullis"

log() { printf '\e[36m[uninstall]\e[0m %s\n' "$*"; }
die() { printf '\e[31m[error]\e[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --purge)    PURGE=1; shift;;
    --purge-go) PURGE_GO=1; shift;;
    -h|--help)  sed -n '2,16p' "$0"; exit 0;;
    *)          die "unknown option: $1";;
  esac
done
[ "$(id -u)" = "0" ] || die "run as root (sudo)."

# ─── stop + disable + remove services ────────────────────────────────────────
for s in $SERVICES; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^$s.service"; then
    log "stopping $s..."
    systemctl disable --now "$s" >/dev/null 2>&1 || true
  fi
  rm -f "/etc/systemd/system/$s.service"
done
systemctl daemon-reload 2>/dev/null || true
# nginx front for the ops app
rm -f /etc/nginx/sites-enabled/portcullis /etc/nginx/sites-available/portcullis
systemctl reload nginx 2>/dev/null || true

# ─── binaries + build tree ───────────────────────────────────────────────────
for b in $BINARIES; do rm -f "$BIN_DIR/$b"; done
log "removed binaries from $BIN_DIR"
rm -rf "$SRC_PARENT" "$PORTCULLIS_DIR"
log "removed build tree $SRC_PARENT and door shim $PORTCULLIS_DIR"

# ─── optional: Go toolchain ──────────────────────────────────────────────────
if [ "$PURGE_GO" = "1" ]; then
  rm -rf "$GO_ROOT"
  log "removed Go toolchain $GO_ROOT"
fi

# ─── optional: config, data, user ────────────────────────────────────────────
if [ "$PURGE" = "1" ]; then
  log "PURGE: removing config, data, logs, shim token/log and service user..."
  rm -rf "$ETC_DIR" "$VAR_DIR" "$LOG_DIR" "$PORTCULLIS_ETC" "$PORTCULLIS_LOG" /var/lib/portcullis
  if id "$UHPPOTED_USER" >/dev/null 2>&1; then userdel "$UHPPOTED_USER" 2>/dev/null || true; fi
  printf '\e[32m[done]\e[0m fully removed (including config + cardholder/web database).\n'
else
  cat <<EOF
$(printf '\e[32m[done]\e[0m') services and binaries removed.
Kept (re-install reuses these): $ETC_DIR, $VAR_DIR, $LOG_DIR, user '$UHPPOTED_USER'.
Run with --purge to remove those too.
EOF
fi
