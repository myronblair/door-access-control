#!/bin/bash
# portcullis-reload-httpd — restart uhppoted-httpd so it reloads its system JSON stores after Ops has
# written to them (the "adopt panel -> DB" reverse sync, or door/group/user edits). Installed root-owned
# at /usr/local/sbin/portcullis-reload-httpd and invoked by the Ops app via a restricted sudo rule.
# Copyright (c) 2026 TomTom Enterprises.
exec systemctl restart uhppoted-httpd
