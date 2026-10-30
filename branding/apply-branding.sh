#!/usr/bin/env bash
# apply-branding.sh — rebrand the uhppoted-httpd web UI as Portcullis (logo, theme, naming).
# Serves the logo as SVG (crisp, real text) and repoints the stock per-theme logo.png references.
# Run on the VM as root after install.sh. Re-runnable.
#   sudo ./apply-branding.sh [/var/uhppoted/httpd/html]
# Copyright (c) 2026 TomTom Enterprises.
set -euo pipefail
HTML="${1:-/var/uhppoted/httpd/html}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# logo (SVG) + favicon
cp "$HERE/portcullis-logo.svg" "$HTML/images/portcullis-logo.svg"
cp "$HERE/portcullis-logo.svg" "$HTML/images/favicon.svg"

# repoint the stock per-theme logo.png references to the SVG
sed -i 's#/images/[^"]*/logo.png#/images/portcullis-logo.svg#g' \
    "$HTML"/sys/*.html "$HTML"/index.html "$HTML"/templates/*.html 2>/dev/null || true

# theme (append once) + logo sizing
grep -q "Portcullis theme" "$HTML/css/uhppoted.css" || cat "$HERE/portcullis-theme.css" >> "$HTML/css/uhppoted.css"
grep -q "login #logo" "$HTML/css/uhppoted.css" || \
    printf '\n.login #logo{height:150px;width:auto}\n#logo{height:52px;width:auto}\n' >> "$HTML/css/uhppoted.css"

# naming: uhppoted-httpd -> Portcullis in page titles/footer (NOT script paths)
sed -i "s/uhppoted-httpd/Portcullis/g" "$HTML"/sys/*.html "$HTML"/index.html "$HTML"/templates/snippets.html 2>/dev/null || true

# add an OPS link into the uhppoted nav so the Phase-B Ops app (/ops) is discoverable (idempotent)
S="$HTML/templates/snippets.html"
grep -q '/ops/">OPS' "$S" 2>/dev/null || \
  sed -i '/\/sys\/users.html/a\          <li><a href="/ops/">OPS</a></li>' "$S" 2>/dev/null || true

chown -R uhppoted:uhppoted "$HTML" 2>/dev/null || true
echo "Portcullis branding applied to $HTML — restart uhppoted-httpd to reload templates."
