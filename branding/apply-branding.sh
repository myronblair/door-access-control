#!/usr/bin/env bash
# apply-branding.sh — rebrand the uhppoted-httpd web UI as Portcullis (logo, theme, naming).
# Run on the VM as root after install.sh. Idempotent-ish (re-appends theme only if missing).
#   sudo ./apply-branding.sh [/var/uhppoted/httpd/html]
# Copyright (c) 2026 TomTom Enterprises.
set -euo pipefail
HTML="${1:-/var/uhppoted/httpd/html}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v convert >/dev/null || { echo "ImageMagick 'convert' required"; exit 1; }

# logo -> all theme variants + favicon
convert -background none "$HERE/portcullis-logo.svg" -resize 400x400 /tmp/portcullis-logo.png
for t in dark light default; do [ -d "$HTML/images/$t" ] && cp /tmp/portcullis-logo.png "$HTML/images/$t/logo.png"; done
cp "$HERE/portcullis-logo.svg" "$HTML/images/favicon.svg"

# theme (append once)
grep -q "Portcullis theme" "$HTML/css/uhppoted.css" || cat "$HERE/portcullis-theme.css" >> "$HTML/css/uhppoted.css"

# naming: uhppoted-httpd -> Portcullis in page titles/footer (NOT script paths)
sed -i "s/uhppoted-httpd/Portcullis/g" "$HTML"/sys/*.html "$HTML"/index.html "$HTML"/templates/snippets.html 2>/dev/null || true

chown -R uhppoted:uhppoted "$HTML" 2>/dev/null || true
echo "Portcullis branding applied to $HTML — restart uhppoted-httpd to reload templates."
