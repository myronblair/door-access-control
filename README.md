# Portcullis — Door Access Control

**Portcullis** is the control system for the 4-door access controller at **10.48.200.61:60000**, replacing the dead OEM
software *iCCard / "Contactless Cards Management System [Access Control & Attendance] V6.9"*
(vendor szwgcn.com, Shenzhen).

## The hardware
- 4-door controller, 2×12V power supply, Wiegand readers.
- **UHPPOTE-family** controller — protocol is 64-byte packets over **UDP 60000**.
- Controller serial **223208728**, IP 10.48.200.61 / 255.255.255.0 / gw 10.48.200.1,
  MAC `00:57:0D:4D:E5:18`.

## Services
All run on the dedicated Ubuntu VM `portcullis` and are fronted by nginx (`:8080`) under one Cloudflare
tunnel. The built-in uhppoted suite provides the access-control core; the `portcullis-*` services add the
history pipeline, the Ops panel, and the anti-phone-home guard.

| Service | Port | Role |
|---------|------|------|
| `uhppoted-httpd` | 8088 | **Access UI** — the browser admin for controllers, doors, cards, groups, users and the live event/audit log. The authoritative store for those entities (`/var/uhppoted/httpd/system/*.json`). Served at `/`. |
| `uhppoted-rest` | 8081 | REST API on the LAN (drives JARVIS voice "open door"); firewalled to the integration hosts. |
| `uhppote-cli` | — | Command-line control + the card-migration load. |
| `portcullis-doorctl` | 8082 | Token-auth door-unlock shim **+ listener guard** (keeps every controller reporting to this host; see below). |
| `portcullis-collector` | — | Polls each controller's event buffer into SQLite and takes a camera snapshot per event. |
| `portcullis-ops` | 8083 | **Ops panel** (see below). Served at `/ops`. |
| `nginx` | 8080 | Front door: `/` → Access UI, `/ops` → Ops panel. |
| `cloudflared` | — | Cloudflare tunnel for the public hostname. |

## The Ops panel (`/ops`)
A Flask app over the event history, adding the operational modules the old iCCard suite had that the
controller itself doesn't provide. **Door, controller, card-holder and keypad names are read live from the
Access UI's store**, so labels never drift between the two UIs — you edit those entities in the Access UI
and they update everywhere in Ops automatically.

| Page | What it does |
|------|--------------|
| **Dashboard** | Totals (events, distinct cards, last event) and the 10 most recent events. |
| **Events** | Access-event history (most recent 500, filterable by days), card-holder name + number, resolved door name, result, event type, and a 📷 link to the snapshot captured at that event. Shows a Controller column once more than one controller is configured. |
| **Attendance** | Per-card, per-day first/last seen and granted counts over a chosen window. |
| **Patrol** | Guard-tour checkpoints (a reader/door) and routes with a time window; shows live which checkpoints were hit on the current run (overnight windows supported). |
| **Meetings** | Meeting definitions (reader + window); attendance = distinct cards granted at that reader in the window. |
| **Cameras** | Hikvision/ISAPI cameras linked to a door (IP, user, password, channel); a camera on a door gets an auto-snapshot when that door fires an event. Includes a one-click credential **test**. |
| **Devices** | One inventory of every peripheral tied to your controllers — cameras, keypads (read live from the Access UI door flags), and manually-added locks, closers, readers and sensors. Includes **ONVIF network discovery** for cameras and an **Add item** form for the wired peripherals that aren't network-discoverable. |
| **Controllers** | Every known controller with its real name and door assignments (from the Access UI store), flagging whether it's registered with the daemons and/or the Access UI. **Network discovery** (UHPPOTE broadcast) to find and add controllers. |
| **Settings** | Read-only mirror of the door names (edited in the Access UI) and the listener-guard status. |

Access to `/ops` is gated by the same Cloudflare Access identity as the rest of the site; every request
must carry the `Cf-Access-Authenticated-User-Email` header (optionally restricted to an allow-list), and
cross-origin form posts are rejected.

## Integrations
- **JARVIS** voice — "open door &lt;n&gt;" calls the door shim (admin-gated) over the LAN.
- **Home Assistant** — door control via the REST API.
- **Blair HQ dashboard** — a Home Automation tile, plus a Backup Status entry.
- **B2 backups** — config + database backed up nightly.

## Deployment
- **VM:** `portcullis` (Ubuntu 24.04) on the Proxmox cluster, static IP on 10.48.200.0/24.
- **Public URL:** `https://portcullis.tomtomenterprises.com` — a Cloudflare tunnel (`cloudflared`) to nginx on the VM.
- **Gated access (internet-facing):** Cloudflare Access (Zero Trust — email OTP/SSO) in front; the Access UI
  additionally has its own login. Direct controller comms stay LAN-only.
- **Install/remove:** `scripts/install.sh` (portable; flags for serial/controller/doors/integrations) and
  `scripts/uninstall.sh` (`--purge` to also drop config + database).

## Repo layout
```
reference/    Vendor docs + wiring diagrams + the old DB schema (SQL, no data)
config/       controller configuration and service units
scripts/      install/uninstall + the portcullis-* services and helpers
integrations/ Home Assistant + JARVIS notes
docs/         runbook / notes
```

## ⚠️ Phone-home to China — found & blocked
The OEM (iCCard) software had configured the controller to send **every access event to an Alibaba
Cloud server in Shenzhen, China** (`120.78.216.117:61009`, 30s heartbeat). The **listener guard** in
`portcullis-doorctl` fixes this: it keeps every configured controller's event listener pointed at **this
host** and re-asserts it within ~60s if it ever drifts, logging any foreign target
(`/var/log/portcullis/guard.log`). Toggle it via the shim API (`/guard/on` · `/guard/off` · `/guard/enforce`,
Bearer-token). Belt-and-suspenders: also deny the controller IP → WAN at the firewall.

## Security
- The controller protocol is **unauthenticated and unencrypted** on the LAN — keep the panel on the
  trusted segment; the Access UI / REST / Ops stay LAN-only (and the public URL behind Cloudflare Access).
- **REST (:8081) and the door shim (:8082)** are firewalled (ufw) to only the integration hosts
  (JARVIS + HA); the shim also requires a bearer token.
- **Card numbers are physical-door credentials.** The old `iCCard.mdb`, the packet capture, and any
  exported card lists live in the **Gitea repo only (never GitHub)** and are git-ignored here.

Copyright (c) 2026 TomTom Enterprises.
