#!/bin/bash
# portcullis-add-controller — append a discovered UHPPOTE controller to uhppoted.conf and reload.
# Installed root-owned at /usr/local/sbin/portcullis-add-controller and invoked by the Ops app via a
# restricted sudo rule (/etc/sudoers.d/portcullis). Both args are strictly validated; nothing from the
# request reaches a shell unquoted.
#   portcullis-add-controller <serial> <ip>
# Copyright (c) 2026 TomTom Enterprises.
set -euo pipefail
SERIAL="${1:-}"; IP="${2:-}"; CONF=/etc/uhppoted/uhppoted.conf
[[ "$SERIAL" =~ ^[0-9]{1,15}$ ]]                 || { echo "bad serial"; exit 2; }
[[ "$IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]     || { echo "bad ip"; exit 2; }
grep -q "UT0311-L0x\.${SERIAL}\.address" "$CONF" && { echo "already present"; exit 0; }
cat >> "$CONF" <<EOF
UT0311-L0x.${SERIAL}.address = ${IP}:60000
UT0311-L0x.${SERIAL}.door.1 = Door1
UT0311-L0x.${SERIAL}.door.2 = Door2
UT0311-L0x.${SERIAL}.door.3 = Door3
UT0311-L0x.${SERIAL}.door.4 = Door4
EOF
systemctl restart uhppoted-httpd uhppoted-rest
echo "added ${SERIAL} @ ${IP}"
