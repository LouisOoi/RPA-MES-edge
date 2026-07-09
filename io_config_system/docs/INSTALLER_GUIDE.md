# Installer Guide — Commissioning a Field IO Terminal

For the person physically standing at the Pi with a laptop on the same
network, no SSH required. Every step below is a real HTTP call against the
endpoints in `api/app.py`; `tests/test_installer_guide.py` runs this exact
sequence against a live (fake-hardware) instance so this document can't
silently drift from what the code actually does.

Replace `http://<terminal-ip>:8080` with the unit's address. Examples use
`curl`; the same calls are what a browser-based UI would make.

## 0. Confirm the unit is alive

```
curl http://<terminal-ip>:8080/api/status
```

No login needed for this one. `{"ok": true}` means the app is running.

## 1. Log in

Ask your supervisor for the admin credentials for a fresh commissioning.
Save cookies across the session (`-c`/`-b`) since every other call needs
the session this creates.

```
curl -c cookies.txt -X POST http://<terminal-ip>:8080/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "admin1", "password": "<admin-password>"}'
```

Response tells you your tier:

```json
{"tier": "admin", "username": "admin1"}
```

## 2. Set identity

Every event this unit ever produces gets stamped with these four fields
plus a `boot_id` you never set yourself — that one is generated once at
the factory and this API will refuse to change it no matter what you send.

```
curl -b cookies.txt -X PUT http://<terminal-ip>:8080/api/identity \
  -H 'Content-Type: application/json' \
  -d '{
    "plant_id": "PLT02", "line_id": "L05", "zone_id": "Z01", "station_id": "ST12",
    "confirm_breaks_continuity": true
  }'
```

`confirm_breaks_continuity` must be `true` — leaving it out or setting it
`false` gets you a `422` with an explanation. This is deliberate: changing
identity on an already-running unit forks its history, and the API won't
let that happen by accident.

## 3. Configure network, MQTT broker, and time

One call sets all three (they live in one file on the device). If your
broker uses TLS (it should), include the cert paths.

```
curl -b cookies.txt -X PUT http://<terminal-ip>:8080/api/system \
  -H 'Content-Type: application/json' \
  -d '{
    "network": {"mode": "static", "ip": "192.168.20.5", "mask": "255.255.255.0",
                 "gateway": "192.168.20.1", "dns": ["192.168.20.1"]},
    "mqtt": {"broker_host": "mqtt.customer.local", "port": 8883, "tls": true,
              "ca_cert": "/etc/certs/ca.crt", "client_cert": "/etc/certs/client.crt",
              "client_key": "/etc/certs/client.key"},
    "time": {"ntp": ["pool.ntp.org"], "timezone": "Asia/Kuala_Lumpur", "rtc_present": true}
  }'
```

**Before committing, test the broker connection** by adding
`?test_only=true` — this checks reachability without saving anything, so
you can fix a typo without leaving the unit half-configured:

```
curl -b cookies.txt -X PUT "http://<terminal-ip>:8080/api/system?test_only=true" \
  -H 'Content-Type: application/json' \
  -d '{ ...same body... }'
```

`{"ok": true}` means go ahead and run the real (non-test_only) call above.

## 4. Find your hardware — Bus Scan

Log in as an operator for the rest of this (an admin session works too —
operator is the minimum, not the maximum).

```
curl -b cookies.txt -X POST http://<terminal-ip>:8080/api/bus/scan \
  -H 'Content-Type: application/json' \
  -d '{"transport": "rtu"}'
```

For a TCP deployment, scan the module subnet instead:

```
curl -b cookies.txt -X POST http://<terminal-ip>:8080/api/bus/scan \
  -H 'Content-Type: application/json' \
  -d '{"transport": "tcp", "ip_range": "192.168.10.10-192.168.10.20"}'
```

Response lists what answered — write down which unit ids (RTU) or IPs
(TCP) are live before you continue; you'll need them in the next step.

## 5. Read the current IO config, then add a point

```
curl -b cookies.txt http://<terminal-ip>:8080/api/io
```

Take that JSON, add your new point(s) and any rule linking them (see
`IO_Config_Execution_Plan.md`'s data model for the exact shape — point
`kind`, `modbus.fn`/`address`, and a rule's `when`/`then`/`else`), and PUT
the whole thing back:

```
curl -b cookies.txt -X PUT http://<terminal-ip>:8080/api/io \
  -H 'Content-Type: application/json' \
  -d '{ ...full io_config with your addition... }'
```

If anything's wrong — a point referencing a device that doesn't exist, two
outputs aliasing the same coil, a rule pointing at a point you deleted —
you get a `422` back with a `problems` list explaining exactly what and
where, and NOTHING gets saved. Fix and retry.

## 6. Verify it's alive

```
curl -b cookies.txt http://<terminal-ip>:8080/api/live
```

Your new point should show up with a `value` and `stale: false`. If it's
missing or stale, check your `unit_id`/`address` against what Bus Scan
found in step 4 before going further.

## 7. Bench-verify the relay — Test Write

This is gated for a reason: it energizes a real output. Two admin-only
steps, then the write itself.

```
curl -b cookies.txt -X POST http://<terminal-ip>:8080/api/commissioning-mode \
  -H 'Content-Type: application/json' -d '{"enabled": true}'

curl -b cookies.txt -X POST http://<terminal-ip>:8080/api/test/write \
  -H 'Content-Type: application/json' \
  -d '{"point": "your_new_relay_id", "value": true, "confirm": true, "timeout_ms": 5000}'
```

Go look at the relay. It auto-reverts to its configured `safe_state` after
`timeout_ms` — you don't have to remember to turn it back off, and neither
does a dropped connection get to leave it energized.

## 8. Clone this setup onto identical units (optional)

```
curl -b cookies.txt http://<terminal-ip>:8080/api/io/export > good_setup.json
```

On the SECOND unit (same login flow, steps 1 and 4 first if it's fresh):

```
curl -b cookies2.txt -X POST http://<second-terminal-ip>:8080/api/io/import \
  -H 'Content-Type: application/json' -d @good_setup.json
```

The second unit keeps its own identity and network settings — import only
touches bus/devices/points/rules, never who this unit is or how it talks
to the broker.

## 9. If something needs undoing

Every save you make is kept, not just the last one:

```
curl -b cookies.txt http://<terminal-ip>:8080/api/config/versions
curl -b cookies.txt -X POST http://<terminal-ip>:8080/api/config/rollback \
  -H 'Content-Type: application/json' -d '{"version": 3}'
```

Leave the body empty (`{}`) to roll back one step instead of to a specific
version number.

## What this guide does NOT cover

- Factory reset — not implemented yet (see `docs/VALIDATION_MATRIX.md`).
- OTA app updates — that's a fleet-management action taken from a central
  system with the signing key, not something you do from the bench; see
  `api_contract.md`'s OTA section if you're building that central tooling.
- The per-terminal **security** commissioning checklist (VLAN, firewall,
  wireless SSID, module hardening) — that's `IO_Config_Execution_Plan.md`'s
  Phase 6 checklist, and it's network/deployment work this API can't do
  for you. Do it before you do step 3 above, not after.
