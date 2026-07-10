# Multi-Zone Guide — Deployment Variant B

For a single terminal (typically a Windows box) supervising several
independent zones — a robot cell, a welding jig, a leak-test rig — each
with its own identity, config, and event history, rather than one
terminal per zone. This is a different deployment shape from everything
in `INSTALLER_GUIDE.md`, which covers Deployment Variant A: one Linux
terminal, one zone. Variant A is unchanged by any of this — it stays the
right choice for a single-zone site.

## What's the same, per zone

Each zone reuses Variant A's engine layer completely unmodified:
`PollEngine`, `RuleEngine`, config hot-reload, the AR-01 through AR-09
remediation (owner/output_class enforcement, the watchdog, per-account
lockout, permit-to-edit, per-slave RTU degradation, config backup
fingerprinting) — all of it applies per zone, exactly as documented for a
single terminal. A zone is not a stripped-down or simplified engine
instance.

## What's different

**One process, N zones.** `engine/zone_orchestrator.py`'s
`ZoneOrchestrator` owns N `PollEngine` instances, one per zone, each on
its own supervised thread. A fault or crash in one zone's poll loop is
caught, logged (`zone_thread_crashed` event, in that zone's own event
log), and that zone's thread restarts with backoff — it never stops or
slows any other zone. Zones are added/removed by `zone_id` and don't
share any engine-level state.

**One file layout, N times.** Each zone is its own directory with the
same four files Variant A already uses:

```
zones/
  weld_cell/
    ctrl_id.json
    system_config.json
    io_config.json
    event_log.db          (created automatically if missing)
  leak_test_rig/
    ctrl_id.json
    system_config.json
    io_config.json
    event_log.db
```

`engine/zone_loader.py`'s `load_all_zones()` discovers every immediate
subdirectory of a `zones/` root and loads each one. A zone directory
missing one of the three required JSON files fails loudly at startup,
naming the broken zone — a bad zone directory stops the whole service
from starting rather than silently running short-handed.

**One Flask app, zone-scoped routes.** `api/multi_zone_app.py`'s
`create_multi_zone_app()` serves every zone from ONE process via
`/api/zone/<zone_id>/...` routes — `io`, `live`, `bus/scan`, `test/write`,
`identity`, `system`, `commissioning-mode`, `config/versions`,
`config/rollback`, `io/export`, `io/import` — the same operations
`api/app.py` exposes for a single terminal, just addressed by zone. An
unknown `zone_id` is a `404 zone_not_found`, not a 422 or 500. Login,
session, and the AR-05 per-account lockout are shared across the whole
terminal — one operator/admin login governs every zone it serves.
`GET /api/zones` is a fleet-view convenience endpoint: which zones exist,
whether each one's thread is running, its crash count and last error.

**`link.medium` per zone.** Each zone's `io_config.json` can set
`"link": {"medium": "wired"}` or `"wireless"` — purely descriptive to the
engine (it never changes Modbus behavior), but
`engine/link_medium.py`'s `recommended_comms_defaults()` hands back
sensible starting timeout/retry/backoff/poll values for whichever medium
a zone uses, for a commissioning tool to pre-fill. A site can mix both:
one zone on a wired switch, another over a wireless gateway, each on its
own recommended defaults, without either zone's settings affecting the
other's.

**Windows Service hosting.** `service/windows_service.py` wraps
`ZoneOrchestrator` as a Windows Service (`SvcDoRun`/`SvcStop` start/stop
every zone's thread). This part is genuinely Windows-only — there is no
Windows machine available to actually run or test it here, so it is
honestly a scaffold: importing the module never fails on Linux/macOS (the
`pywin32` dependency is guarded), but running it as an installed service
is unverified. `build_orchestrator()` and `load_all_zones()`/
`load_zone_from_directory()` underneath it are fully tested on any OS —
see `tests/test_zone_orchestrator.py` and `tests/test_zone_loader.py`.
COM-port serial device paths (`"COM3"` etc., vs. Linux's `/dev/ttyUSB0`)
need no code change — `bus.serial.port` is just a string pymodbus passes
through.

## VLAN / network isolation

The Phase 6 security checklist (VLAN isolation, firewall rules, wireless
SSID hardening, module hardening) applies **per zone**, and applies
identically whether that zone's `link.medium` is `"wired"` or
`"wireless"` — isolating the Modbus segment is a property of the network
topology, not of the physical link. Do this before commissioning each
zone's `io_config.json`, not after.

## What's still not built

- A real Windows Service install/start/stop cycle has never been run
  against real Windows hardware.
- No mixed-fleet field test against real RTU/TCP devices — the
  regression test proving wired/wireless settings don't bleed
  (`tests/test_multi_zone_app.py`) runs against fake Modbus clients.
- Zone-scoped OTA routes don't exist yet — Phase 7's OTA API
  (`api/app.py`'s `/api/ota/*`) is still single-terminal only.
