# Portcullis — Runbook

Door access control for the 4-door UHPPOTE panel, replacing the dead iCCard v6.9 software.

## Components
- **VM `portcullis`** — VMID 119 on PVE1, **10.48.200.67**, Ubuntu 24.04 (2 vCPU / 2 GB / 20 GB). SSH key: `/root/.ssh/portcullis` on PVE1 (`ssh -i … portcullis@10.48.200.67`).
- **Controller** — serial **223208728**, 10.48.200.61:60000, firmware v6.62. UHPPOTE protocol (UDP 60000).
- **uhppoted stack** (built from source, systemd):
  - `uhppoted-httpd` — web UI, HTTP **:8080** (localhost; TLS is on Cloudflare).
  - `uhppoted-rest` — REST API **:8081** (reads/writes; firewalled to JARVIS + HA).
  - `uhppote-cli` — CLI (`uhppote-cli --config /etc/uhppoted/uhppoted.conf …`).
- **`portcullis-doorctl`** — custom token-auth shim, **:8082** (firewalled to JARVIS + HA). `POST /open/<1-4>` with `Authorization: Bearer <token>` → cardless unlock. Token at `/etc/portcullis/doorctl.token`.

## Access
- **Web UI:** https://portcullis.tomtomenterprises.com — Cloudflare Tunnel (no open ports) + Cloudflare Access (email OTP) + the app's own admin login. No port to remember.
- Admin user created via the gated setup page; setup page now disabled (`httpd.security.no-setup = true`).

## Operating
- **Open a door (cardless):** `uhppote-cli --config /etc/uhppoted/uhppoted.conf open 223208728 <door>` — or `POST :8082/open/<door>` with the bearer token (how HA/JARVIS do it).
- **Read status/cards:** `uhppote-cli … get-status 223208728` / `get-cards 223208728`.
- **Manage cardholders:** the web UI (syncs from the controller).
- **Cards today:** 7 enrolled on the panel, all Door 1+2 only (D3/D4 unused). Export: `/var/uhppoted/migration/panel-cards.txt`. No cardholder names exist (the panel never stored them; the old mdb was empty).
- **Card type:** **125 kHz EM4100** proximity cards/fobs (confirmed from a real swipe event + the sequential-batch hex pattern `0154xxxx`). Readers output the **full card ID (Wiegand-34)**. Buy UHPPOTE 125 kHz EM4100/TK4100 cards — test one pack first.

### Enroll a new card (reliable method, matches the readers' format)
1. Tap the new card at any **door reader** (it'll be denied — that's expected).
2. Read the exact number the controller just recorded — in the web UI **Events**, or `uhppote-cli --config /etc/uhppoted/uhppoted.conf get-event 223208728`.
3. Add that number to a cardholder in the web UI (with door access + validity dates).

A USB 125 kHz reader (e.g. UHPPOTE B014SACF9Q) also works for desk enrollment, but confirm it reports the same full number before trusting it for a batch.

## Integrations
- **Home Assistant:** `integrations/home-assistant.yaml` (rest_command + door scripts calling the shim). HA (.97) is firewall-allowed. Needs the shim token in HA `secrets.yaml`.
- **JARVIS voice:** `integrations/jarvis.md` (kb_intents row + handler + config constants). JARVIS (.211) is firewall-allowed and already has HA integration.

## Ops
- **Backups:** B2 nightly (`/etc/uhppoted` + `/var/uhppoted` incl. cards/web DB) via `b2-site-backup`, cron 17:00, GPG-encrypted.
- **Monitoring:** JARVIS agent (registered to 10.48.200.211); qemu-guest-agent for Proxmox.
- **Dashboard:** tile in Blair HQ → Home Automation card.

## Security model
- Controller protocol is unauthenticated on the LAN → panel stays on the trusted segment.
- REST (:8081) and shim (:8082) are **firewalled (ufw) to only JARVIS (.211) and HA (.97)**; shim also requires a bearer token.
- Web UI is reachable only through Cloudflare Access (email OTP). No inbound ports open to the internet (outbound tunnel).
- Card numbers / the shim token / the old mdb / the pcap are **Gitea-only / on-host**, never GitHub.

## Reproduce / rebuild
1. `scripts/install.sh` — uhppoted stack + config + hardened systemd units (validated on this VM).
2. `branding/apply-branding.sh` — Portcullis logo + theme + naming.
3. `scripts/doorctl.py` + `scripts/portcullis-doorctl.service` — the shim (create `/etc/portcullis/doorctl.token` with `openssl rand -hex 24`, chmod 600, `uhppoted`-owned; ufw-allow 8082 from .211/.97).
4. Cloudflare Tunnel: `cloudflared service install <token>`; add Public Hostname + Access app in the dashboard.

## Phase B — reporting modules
Built on a shared **event pipeline**: `portcullis-collector` polls the controller's event buffer into
SQLite (`/var/lib/portcullis/portcullis.db`, table `events`) every 30s — durable, backfills history,
survives the controller flapping.
- **Event pipeline** — DONE (`scripts/collector.py`, service `portcullis-collector`; 18 historical events captured).
- **Attendance** — DONE (core): `scripts/report.py` → per-card, per-day first-seen/last-seen/counts.
  `sudo -u uhppoted python3 /opt/portcullis/report.py [--days N] [--json]`.
- **Patrol / Meeting-sign** — TODO: need your definitions (which readers are patrol checkpoints + routes/schedules; meeting list). Both are queries over the same `events` table.
- **Hikvision cameras** — TODO: via ISAPI/RTSP on Linux (needs camera IPs + creds); link a camera to a door, snapshot on event.
- **Web views** — TODO: attendance/patrol are CLI/JSON today; surfacing them in the gated web UI needs a small page served behind the Cloudflare tunnel.

## Pending / decisions
- **Door names** — confirm which physical door is 1/2/3/4 (improves HA/JARVIS phrasing). Only 1+2 are in use.
- **HA activation** — paste `integrations/home-assistant.yaml` (HA has no non-interactive SSH from here).
- **JARVIS voice — DONE**: `open_door` intent deployed (chat.php + kb_intents + config constants, PHP restarted); admin-gated; say "open door N". Calls the shim (firewall + /health verified). First real actuation = the after-hours test.
- **First real door-open** — the after-hours actuation test (nothing has actuated a door yet).
- **Phase B** — Attendance / Patrol / Meeting-sign / Hikvision camera modules (see the plan).

Copyright (c) 2026 TomTom Enterprises.
