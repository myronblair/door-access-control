# Door Access Control

Control system for the 4-door access controller at **10.48.200.61:60000**, replacing the dead OEM
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

Integrations: Blair HQ dashboard tile, Home Assistant (4 door entities), JARVIS voice control.

## Repo layout
```
reference/    Vendor docs + wiring diagrams + the old DB schema (SQL, no data)
config/       uhppoted.conf and service units
scripts/      migration + helper scripts
docs/         runbook / notes
```

## Security
- The controller protocol is **unauthenticated and unencrypted** on the LAN — keep the panel on the
  trusted segment; web UI / REST stay home + LAN only behind NPM.
- **Card numbers are physical-door credentials.** The old `iCCard.mdb`, the packet capture, and any
  exported card lists live in the **Gitea repo only (never GitHub)** and are git-ignored here.

Full plan: `~/.claude/plans/toasty-humming-jellyfish.md`.

Copyright (c) 2026 TomTom Enterprises.
