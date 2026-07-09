# Field IO Terminal — Programmable Configuration System

**Execution Plan v2 · July 2026**
Companion to `factory_iot_reference.md`. Scope: a **shippable standard product** — firmware + local web UI that lets a deployment team or customer commission and reconfigure an edge IO terminal end to end: set its identity, network and MQTT broker, define what's on the Modbus bus (RS485 or TCP), name/scale points, wire sensors to relays with logic rules, and receive updates over the air — all through a browser, no SSH, no code edits.

---

## Decisions locked in

| Decision | Choice | Consequence |
|---|---|---|
| Where the UI runs | **On each edge Pi (local)** | Flask app per terminal on the LAN. No central dependency. Config lives on the Pi. |
| Config scope | **Mapping + logic rules** | IO point definitions *and* an on-device rule engine (IF condition → relay action). |
| Applying changes | **Hot reload, no restart** | Poll loop swaps config live after validation. Bad config is rejected; the running config keeps going. |
| First deliverable | **Plan + clickable mockup** | This document + `io_config_mockup.html`. No production code until you approve. |
| Auth | **Two tiers: operator + admin** | Operator password for day-to-day IO/rules; separate admin/installer password gates identity, network, bus transport, factory reset, OTA. Username stamped on every change for the audit trail. |
| Rule engine v1 | **Single-condition IF→THEN** | One condition per rule now. Schema and evaluator structured so multi-condition (AND/OR), timers and counters drop in later without a rewrite. |
| Analog v1 | **Digital inputs + PLC registers only** | No analog scaling in v1. The `scaling`/`unit` fields stay in the schema (reserved) so analog is a UI-enable later, not a data-model change. |
| Config portability | **Export / import file** | A validated config can be exported and imported onto identical terminals. |
| Identity | **Editable, but gated** | plant/line/zone/station set via first-boot wizard (easy) and changeable only behind admin + a "breaks historical continuity" warning + an identity-change event. **`boot_id` stays system-locked** (reference Rule 2). |
| Network / broker | **Configured in-app (no SSH)** | Device IP/DHCP, MQTT broker host/port, and TLS cert upload done through the UI so the deployment team commissions a unit without a terminal. |
| Transport | **RS485 RTU or Modbus TCP** | TCP: edge is client, each IO module is its own server/IP (Method A wired switch or B wireless gateway, per site). |
| Updates | **Remote / OTA push** | App + config-schema updates pushed to the fleet, with schema migration so shipped units upgrade cleanly. |
| Time | **NTP/RTC configured per unit** | `controller_ts` drives all metrics (reference Rule 4); a wrong clock corrupts them, so clock setup is part of commissioning. |

---

## The core problem

Today `modbus_poll.py` hardcodes everything: slave addresses, `BUTTON_COIL`, `LED_COIL`, `FAULT_REGISTER`, debounce, and the button→LED logic. Every site change means editing Python on the Pi. The configuration system lifts all of that out of code into a **validated config file the customer edits through a browser**, and makes the poll loop read that file instead of constants.

Two hard constraints inherited from the reference, non-negotiable:

- **`boot_id` stays system-locked.** The UI may edit the *location* identity (plant/line/zone/station) in `/etc/ctrl_id.json`, but never `boot_id` — it is generated only at provisioning / factory reset and must survive SQLite recreation (reference Rule 2). Editing it would reintroduce the boot_id data-loss bug.
- **A misconfigured coil write can energize a real relay.** This is industrial output. Safety gating (test mode, confirmations, safe-state on bad config) is a first-class requirement, not polish.

### Config files on the device (three, separated by change-cadence and blast radius)

| File | Holds | Who edits | Blast radius |
|---|---|---|---|
| `/etc/ctrl_id.json` | plant/line/zone/station + **locked** `boot_id` | Admin (wizard / gated) | Changes MQTT topic + server key — forks history |
| `/etc/system_config.json` | network (IP/DHCP), MQTT broker host/port, TLS cert refs, NTP | Admin | Connectivity to broker |
| `/etc/io_config.json` | bus, devices, points, rules | Operator | Field IO behaviour (hot-reloaded) |

Keeping them separate means an everyday IO edit can never accidentally rewrite identity or network, and each has its own version/rollback.

---

## Target architecture (per terminal)

```
┌──────────────────────────────────────────────────────────────┐
│  Raspberry Pi (edge terminal)                                 │
│                                                                │
│  ┌────────────────┐     watches      ┌──────────────────────┐ │
│  │ Flask config   │  writes atomic   │ /etc/io_config.json  │ │
│  │ web app :8080  │─────────────────▶│ (versioned + LKG)    │ │
│  │ (UI + REST)    │                  └──────────┬───────────┘ │
│  └───────┬────────┘                             │ mtime/vers  │
│          │ live values (read)          reload   │ watch       │
│          ▼                                       ▼             │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Config-driven poll engine (replaces modbus_poll.py)     │  │
│  │  · reads point map  · runs rule engine  · writes coils  │  │
│  └───────┬───────────────────────────────┬────────────────┘  │
│          │ Modbus RTU (pymodbus)          │ log_event()       │
│          ▼                                ▼                    │
│     RS485 bus                        SQLite event_log ─▶ MQTT  │
└──────────────────────────────────────────────────────────────┘
```

The web app and the poll engine are **separate processes** that share only the config file (single source of truth) and a read-only live-values snapshot. This keeps a browser crash or a bad HTTP request from ever stalling the Modbus loop.

---

## Config data model (the foundation)

Everything depends on getting this schema right. Draft shape of `/etc/io_config.json`:

**`/etc/ctrl_id.json` (identity — admin only):**

```jsonc
{
  "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
  "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"  // LOCKED — never UI-editable
}
```

**`/etc/system_config.json` (network / broker / time — admin only):**

```jsonc
{
  "network": { "mode": "static", "ip": "192.168.10.9", "mask": "255.255.255.0",
               "gateway": "192.168.10.1", "dns": ["192.168.10.1"] },
  "mqtt":    { "broker_host": "mqtt.customer.local", "port": 8883, "tls": true,
               "ca_cert": "/etc/certs/ca.crt", "client_cert": "/etc/certs/client.crt",
               "client_key": "/etc/certs/client.key" },
  "time":    { "ntp": ["pool.ntp.org"], "timezone": "Asia/Kuala_Lumpur", "rtc_present": true }
}
```

**`/etc/io_config.json` (IO — operator, hot-reloaded):**

```jsonc
{
  "schema_version": 2,               // product schema; drives OTA migration
  "config_version": 7,               // monotonic; bumped on every save
  "updated_at": 1752000000000,
  "updated_by": "operator@site",
  "bus": {
    "transport": "rtu",            // "rtu" (RS485 serial) | "tcp" (Modbus TCP)
    "poll_interval_ms": 100,
    // used when transport == "rtu"
    "serial": { "port": "/dev/ttyS0", "baudrate": 19200,
                "parity": "N", "stopbits": 1, "bytesize": 8 },
    // TCP defaults; each device carries its OWN host (see below)
    "tcp": { "port": 502, "timeout_ms": 800, "retries": 2, "backoff_ms": 200 }
  },
  "devices": [
    // RTU: unit_id = RS485 slave address, no host.
    // TCP: each module is its own server → host is REQUIRED per device.
    { "unit_id": 1, "name": "IO Module A", "type": "remote_io",
      "tcp": { "host": "192.168.10.11" } },
    { "unit_id": 1, "name": "IO Module B", "type": "remote_io",
      "tcp": { "host": "192.168.10.12" } }
  ],
  "points": [
    {
      "id": "btn_maint",              // stable id used by rules
      "name": "Maintenance Button",
      "unit_id": 1,
      "kind": "digital_in",          // digital_in|digital_out|analog_in|register
      "modbus": { "fn": "read_coils", "address": 0, "count": 1 },
      "scaling": null,               // analog only
      "unit": null,
      "debounce_ms": 300,
      "invert": false
    },
    {
      "id": "led_maint",
      "name": "Maintenance LED",
      "unit_id": 1,
      "kind": "digital_out",
      "modbus": { "fn": "write_coil", "address": 1 }
    },
    {
      "id": "temp_oven",
      "name": "Oven Temperature",
      "unit_id": 5,
      "kind": "analog_in",
      "modbus": { "fn": "read_input_registers", "address": 0, "count": 1 },
      "scaling": { "raw_min": 0, "raw_max": 4095, "eng_min": 0, "eng_max": 200 },
      "unit": "°C"
    }
  ],
  "rules": [
    {
      "id": "rule_button_led",
      "enabled": true,
      // v1: exactly one condition in this array. Future: multiple + "match":"all|any"
      "match": "all",
      "when": [{ "point": "btn_maint", "op": "rising" }],
      "then": [
        { "action": "pulse", "point": "led_maint", "ms": 500 },
        { "action": "log_event", "event_type": "maintenance_request" }
      ],
      "else": []
    }
  ]
}
```

Design notes that matter:

- **Points have stable string IDs.** Rules reference points by `id`, never by raw address, so re-addressing hardware doesn't silently break logic.
- **Scaling is declarative** (raw range → engineering range). The poll engine applies it; the UI shows engineering units.
- **The rule engine is data, not code.** No `eval`. A fixed, whitelisted set of operators (`>`, `<`, `>=`, `<=`, `==`, `between`, `rising`, `falling`) and actions (`set`, `pulse`, `log_event`). This is the only safe way to accept logic from a browser.
- **`when` is an array with `match`, even though v1 allows only one condition.** The evaluator loops the array and applies `match` (`all`=AND / `any`=OR). v1 validation caps the array at length 1; lifting that cap plus adding timer/counter operators is the *only* change needed for multi-condition logic later — no schema migration.
- **Analog is reserved, not wired.** `scaling`/`unit` stay in the point schema and the `analog_in` kind is defined, but v1 validation rejects `analog_in` points. Enabling analog later is a UI toggle + removing that validation guard, not a data-model change.
- **Every save is attributed.** `updated_by` carries the username typed at login (audit only). Access is gated by two passwords: **operator** (IO/rules) and **admin** (identity, network, bus transport, factory reset, OTA). Destructive actions require the admin password even within a session.
- **`config_version` drives hot reload and rollback.** Last-known-good (LKG) config is retained so a bad save can auto-revert.
- **`schema_version` drives OTA migration.** It is the *product* schema version, distinct from the per-save `config_version`. When an OTA update ships a newer schema, a migration step upgrades the on-device config from its `schema_version` to the new one before the new app runs. Migrations are forward-only, tested, and backed up first.
- **Identity edits emit an `identity_change` event.** Changing plant/line/zone/station is allowed (admin + confirm) but is logged as an event to the server so the history fork is explicit and traceable — never silent. `boot_id` is never touched here.
- **Transport is a config choice: RS485 (Modbus RTU) or Modbus TCP.** `pymodbus` provides both `ModbusSerialClient` and `ModbusTcpClient`, so the poll engine picks the client at load time from `bus.transport`. Everything above the client — point map, scaling, rules, hot reload — is transport-agnostic. `unit_id` is the RS485 slave address for RTU and the unit/slave id for TCP (usually `1` for a native TCP IO module).
- **TCP topology (resolved):** the edge Pi is the **Modbus TCP client**; each IO module is its **own TCP server with its own IP**. So in TCP mode **`tcp.host` lives on the device, not the bus** — one `ModbusTcpClient` connection per module IP, pooled and reused. Method A (Pi → switch → modules) and Method B (Pi → wireless gateway → modules) are identical at the Modbus layer; they differ only in link reliability. **Method B needs a longer `timeout_ms` and a retry/backoff policy** because wireless drops and jitter are normal — the poll engine treats a timed-out read as "stale", not "zero", so a Wi-Fi hiccup never fakes a sensor value or mis-fires a rule.
- **Bus Scan is transport-aware:** RTU scans unit ids 1–32 on the serial line; TCP pings a user-supplied IP range (or list) on port 502 and reports which modules answer.

---

## Phased plan

### Phase 0 — Schema, contracts & migration framework *(no hardware)*
Finalize the three schemas (identity, system, io), write JSON-Schema validators, and define the REST contract (`GET/PUT /api/io`, `GET/PUT /api/system`, `GET/PUT /api/identity`, `GET /api/live`, `POST /api/bus/scan`, `POST /api/test/write`, `GET /api/status`, `POST /api/factory-reset`). Stand up the **schema-migration framework** (`schema_version` → forward-only migrations, backup-first) now, because shipped units depend on it. Deliverable: schema files + validators with unit tests + a seed config reproducing the *current* hardcoded behaviour exactly. **Exit test:** seed config, run through the Phase-2 engine, behaves identically to today's `modbus_poll.py`; a v1→v2 migration test passes.

### Phase 1 — Local web UI + first-boot wizard *(mockup first — this delivery)*
Flask app serving all screens plus a **first-boot provisioning wizard** (identity → network/broker → time → bus → points) that walks an installer through a factory-fresh unit. Two-tier login (operator/admin). The clickable mockup (`io_config_mockup.html`) is built now so you react to the UX before real code. **Exit test:** you sign off on the screen flow and wizard sequence.

### Phase 2 — Config-driven poll engine
Refactor `modbus_poll.py` into an engine that builds its poll plan from `points[]` (RTU or TCP client chosen from `bus.transport`) instead of constants — grouped reads per device, results to a shared live snapshot, timed-out TCP reads marked stale, and `log_event()` still stamping all 6 identity fields. **Exit test:** side-by-side run against current code produces identical events for the same bus activity.

### Phase 3 — Rule engine *(single-condition v1, extensible)*
Evaluate `rules[]` each cycle: whitelisted operators, edge detection, actions that write coils / emit events, every coil write logged. v1 caps `when` at one condition; the loop + `match` (all/any) structure is built now so AND/OR, timers and counters are a later drop-in. **Exit test:** unit tests per operator + a "button→LED" rule reproducing the reference's fixed logic purely from config.

### Phase 4 — Hot reload (no restart)
Engine watches `config_version`. On change: validate → build new poll plan → atomically swap → keep old plan if validation fails. Outputs move to a per-point **safe state** during the swap. Atomic writes (temp+rename) so a crash mid-save never yields a half file. **Exit test:** change config under live polling — no missed cycles, no relay glitch, invalid save rejected with running config untouched.

### Phase 5 — Identity, network & broker commissioning *(admin tier)*
The screens that make it a shippable product: **Identity** (edit plant/line/zone/station behind admin + "breaks historical continuity" confirm + `identity_change` event; `boot_id` shown read-only), **Network** (static IP/DHCP), **MQTT broker** (host/port + TLS cert upload with a "Test connection" button), and **Time** (NTP servers, timezone, RTC status). A privileged helper applies OS-level network/time changes; the app only writes validated intent. **Exit test:** a fresh unit is commissioned end-to-end through the browser — identity set, joins the customer broker over TLS, clock synced — with no SSH.

### Phase 6 — Commissioning & safety tools *(operator tier)*
**Bus Scan** (RTU unit ids / TCP IP range), **Live Values** (read-only identification view), **Test Write** (manual output pulse gated behind commissioning mode + confirm + auto-timeout), **version history + one-click rollback**, and **export / import** to clone a good setup onto identical units. **Exit test:** a non-programmer adds a digital point, links it to a relay, verifies it on the bench, exports the config, and imports it onto a second unit.

### Phase 7 — OTA updates & fleet management
Signed OTA push of app + schema updates: staged download → verify signature → backup current → migrate config `schema_version` → atomic swap → health check → auto-rollback on failure. Per-unit update status reported centrally. **Exit test:** push an update carrying a schema change to a test unit; verify migration, health check, and that a deliberately failing update auto-rolls-back to the prior version and config.

### Phase 8 — Validation, security checklist & handover
Full validation matrix (address conflicts, dangling rule refs, output contention, reload under load, offline edits, identity-change fork handling), the per-terminal **security commissioning checklist** (below), and customer/installer docs. **Exit test:** the reference's Phase-1 controller checklist still passes on the new stack; a clean-room installer commissions a unit from the docs alone.

---

## Risks & how the plan handles them

| Risk | Mitigation |
|---|---|
| Bad config energizes a relay unsafely | Whitelisted rule engine (no eval), per-point safe-state on reload, test writes gated behind commissioning mode with timeout |
| Half-written config file after crash | Atomic write (temp+rename) + JSON-Schema validation before swap + retained LKG |
| Browser/HTTP fault stalls Modbus | Web app and poll engine are separate processes sharing only files |
| Rule references a deleted point | Validation blocks the save; dangling references reported in the UI |
| Two rules fight over one coil | Output-contention check at validation time |
| `boot_id` edited/lost | Never UI-editable; lives in `/etc/ctrl_id.json`, survives SQLite recreation (reference Rule 2) |
| Identity changed → history silently forks | Admin-gated + explicit "breaks continuity" confirm + `identity_change` event to server; not silent |
| OTA update bricks a shipped unit | Signature verify, backup-first, health check, auto-rollback to prior app+config |
| Wrong clock corrupts all metrics | NTP/RTC configured at commissioning; `controller_ts` used everywhere (reference Rule 4) |
| Everyday edit rewrites identity/network | Three separate config files; operator tier can't touch identity/system config |

---

## Resolved decisions

1. **Auth** — two tiers: operator (IO/rules) + admin (identity, network, bus transport, factory reset, OTA); username stamped on every save.
2. **Rule engine** — single-condition IF→THEN in v1; schema (`when[]` + `match`) built to extend to AND/OR, timers, counters without migration.
3. **Analog** — digital inputs + PLC registers only in v1; `scaling`/`unit` and `analog_in` reserved for a later UI-enable.
4. **Portability** — export / import of a validated config file (Phase 6).
5. **Identity** — plant/line/zone/station editable via first-boot wizard and gated post-commissioning; `boot_id` locked.
6. **Network/broker** — device IP, MQTT broker, TLS certs, and NTP/time all configured in-app (Phase 5).
7. **Transport** — RS485 RTU or Modbus TCP (edge=client, per-device IP); Methods A & B per site.
8. **Updates** — signed OTA push with schema migration and auto-rollback (Phase 7).

## TCP security (guidance — pick a posture)

**The uncomfortable fact first:** Modbus TCP has *no authentication and no encryption*. Anyone who can reach an IO module's IP on port 502 can read every sensor and — worse — **write coils and registers**, i.e. flip your relays. The protocol will not stop them; there is no password. Security is therefore a *network-design* problem, not something the config app can fix. Method B (wireless) is the higher-risk path: if the plaintext Modbus traffic rides Wi-Fi, anyone in radio range who joins or cracks that link can command your outputs.

Defense is layered. From most to least important for your setup:

1. **Isolate the OT network (do this regardless).** Put the edge Pi and all IO modules on a **dedicated VLAN / subnet with no route to the office network or the internet**. The modules should be reachable *only* from the edge Pi. This single step removes ~90% of the risk because it removes the attackers who could reach port 502 in the first place.
2. **Firewall the edge Pi.** Allow outbound to module IPs on port 502 only; drop everything else. The Pi already reaches the outside world only through the MQTT/TLS channel in the reference — keep that the sole crossing point between OT and IT.
3. **Secure the wireless link (Method B only, mandatory).** WPA2/WPA3 on a dedicated SSID for the OT VLAN, strong pre-shared key, not shared with staff/guests. Consider MAC allow-listing on the gateway. Treat the radio as the perimeter.
4. **Lock down the modules themselves.** Change default web-config passwords, disable unused services/ports, and pin static IPs (DHCP reservations) so scan/allow-lists stay valid.
5. **TLS tunnel only if you need OT↔IT crossing.** Native "Modbus/TCP Security" (TLS) exists but almost no field modules support it. If a module must be reached across networks, wrap the link in a VPN or stunnel rather than exposing port 502.

**Your posture (resolved): both methods per site.** Every TCP terminal gets items 1, 2, 4 (VLAN + firewall + module hardening). Wireless sites additionally get item 3 (dedicated OT SSID, WPA2/WPA3). The Phase 6 checklist has a "wireless?" branch so installers apply #3 only where Method B is used.

**What this means for the build:** none of it is application code — it's deployment/network config. The plan's part is (a) a **commissioning checklist** documenting the required VLAN/firewall/Wi-Fi setup per terminal, and (b) making the config app **bind only to the OT interface** and require login before any write. Flagged in Phase 6.

### Phase 6 — per-terminal security commissioning checklist

Applied at install, recorded per terminal:

- [ ] Edge Pi + all IO modules on a dedicated OT VLAN/subnet, no route to office LAN or internet
- [ ] Modules reachable **only** from the edge Pi (switch ACL or firewall rule)
- [ ] Edge firewall: outbound allowed to module IPs on port 502 only; MQTT/TLS is the sole OT↔IT crossing
- [ ] Module web-config default passwords changed; unused services disabled
- [ ] Static IPs pinned (DHCP reservations) so scan/allow-lists stay valid
- [ ] Config app bound to the OT interface only; login required before any write
- [ ] **If wireless (Method B):** dedicated OT SSID, WPA2/WPA3, strong PSK not shared with staff/guests; MAC allow-list on gateway
- [ ] Record method (A/B), VLAN id, and module IPs in the terminal's commissioning sheet

## All decisions resolved

Nothing outstanding. Ready to start Phase 0 on your go.
