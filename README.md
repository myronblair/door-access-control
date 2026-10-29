# Portcullis — Door Access Control

**Portcullis** is the control system for the 4-door access controller at **10.48.200.61:60000**, replacing the dead OEM
software *iCCard / "Contactless Cards Management System [Access Control & Attendance] V6.9"*
(vendor szwgcn.com, Shenzhen).

## The hardware
- 4-door controller, 2×12V power supply, Wiegand readers.
- **UHPPOTE-family** controller — protocol is 64-byte packets over **UDP 60000**, fully implemented by
  the open-source [uhppoted](https://github.com/uhppoted) suite.
- Controller serial **223208728**, IP 10.48.200.61 / 255.255.255.0 / gw 10.48.200.1,
  MAC `00:57:0D:4D:E5:18`.

## Solution
Web-driven service on a dedicated Ubuntu VM, built on uhppoted:

| Component | Role |
|-----------|------|
| `uhppoted-httpd` | Browser admin UI — cardholders, doors, groups, access rights, live events |
| `uhppoted-rest` | REST API (drives JARVIS voice "open door") |
| `uhppoted-mqtt` | Publishes door state/events to Home Assistant's MQTT broker |
| `uhppote-cli` | Scripting + the card migration load |
| `portcullis-doorctl` | Token-auth door-unlock shim **+ listener guard** (keeps the controller reporting to this host; see below) |

Integrations: Blair HQ dashboard tile, Home Assistant (4 door entities), JARVIS voice control.

## Deployment
- **VM:** `portcullis` (Ubuntu) on the Proxmox cluster.
- **Public URL:** `https://portcullis.tomtomenterprises.com` — Cloudflare-proxied, reverse-proxied by NPM to the VM's `uhppoted-httpd`.
- **Gated access (internet-facing):** Cloudflare Access (Zero Trust — email/SSO) in front, **plus** the app's own login = defense in depth. Direct controller comms stay LAN-only.

## Repo layout
```
reference/    Vendor docs + wiring diagrams + the old DB schema (SQL, no data)
config/       uhppoted.conf and service units
scripts/      migration + helper scripts
docs/         runbook / notes
```

## ⚠️ Phone-home to China — found & blocked
The OEM (iCCard) software had configured the controller to send **every access event to an Alibaba
Cloud server in Shenzhen, China** (`120.78.216.117:61009`, 30s heartbeat). The **listener guard** in
`portcullis-doorctl` fixes this: it keeps the controller's event listener pointed at **this host**
("reroute back into itself") and re-asserts it within ~60s if it ever drifts, logging any foreign
target (`/var/log/portcullis/guard.log`). Toggle it on/off via the shim API (`/guard/on` · `/guard/off`
· `/guard/enforce`, Bearer-token). Belt-and-suspenders: also deny the controller IP → WAN at the firewall.

## Security
- The controller protocol is **unauthenticated and unencrypted** on the LAN — keep the panel on the
  trusted segment; web UI / REST stay home + LAN only behind NPM.
- **REST (:8081) and the door shim (:8082)** are firewalled (ufw) to only the integration hosts
  (JARVIS + HA); the shim also requires a bearer token.
- **Card numbers are physical-door credentials.** The old `iCCard.mdb`, the packet capture, and any
  exported card lists live in the **Gitea repo only (never GitHub)** and are git-ignored here.

Full plan: `~/.claude/plans/toasty-humming-jellyfish.md`.

Copyright (c) 2026 TomTom Enterprises.
