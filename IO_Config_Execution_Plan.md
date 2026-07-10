# Field IO Terminal — Programmable Configuration System

**Execution Plan v2 · July 2026**
Companion to `factory_iot_reference.md`. Scope: a **shippable standard product** — firmware + local web UI that lets a deployment team or customer commission and reconfigure an edge IO terminal end to end: set its identity, network and MQTT broker, define what's on the Modbus bus (RS485 or TCP), name/scale points, wire sensors to relays with logic rules, and receive updates over the air — all through a browser, no SSH, no code edits.

---

## Decisions locked in

| Decision | Choice | Consequence |
|---|---|---|
| Where the UI runs | **On each edge Pi (local)** | Flask app per terminal on the LAN. No central *control* dependency — the device works standalone. **Amended (AR-09):** every successful config apply also pushes a non-secret copy to a central backup store over the existing MQTT/TLS channel, for fleet drift reporting and RMA restore. Backup is one-way and best-effort; the device never depends on the server to function. |
| Config scope | **Mapping + logic rules** | IO point definitions *and* an on-device rule engine (IF condition → relay action). |
| Applying changes | **Hot reload, no restart** | Poll loop swaps config live after validation. Bad config is rejected; the running config keeps going. |
| First deliverable | **Plan + clickable mockup** | This document + `io_config_mockup.html`. No production code until you approve. |
| Auth | **Real per-user accounts, roles = operator/admin** *(revised — was two shared passwords)* | **Amended (AR-05):** shared passwords and a self-typed audit username are replaced with real per-user accounts; role (operator/admin) is assigned per account, not shared. Session lockout/backoff on repeated failed logins. Config UI served over TLS. Audit log is bound to the authenticated account, not a typed string. |
| Rule engine v1 | **Single-condition IF→THEN** | One condition per rule now. Schema and evaluator structured so multi-condition (AND/OR), timers and counters drop in later without a rewrite. |
| Analog v1 | **Digital inputs + PLC registers only** | No analog scaling in v1. The `scaling`/`unit` fields stay in the schema (reserved) so analog is a UI-enable later, not a data-model change. |
| Config portability | **Export / import file** | A validated config can be exported and imported onto identical terminals. |
| Identity | **Editable, but gated** | plant/line/zone/station set via first-boot wizard (easy) and changeable only behind admin + a "breaks historical continuity" warning + an identity-change event. **`boot_id` stays system-locked** (reference Rule 2). |
| Network / broker | **Configured in-app (no SSH)** | Device IP/DHCP, MQTT broker host/port, and TLS cert upload done through the UI so the deployment team commissions a unit without a terminal. |
| Transport | **RS485 RTU or Modbus TCP** | TCP: edge is client, each IO module is its own server/IP (Method A wired switch or B wireless gateway, per site). |
| Updates | **Remote / OTA push** | App + config-schema updates pushed to the fleet, with schema migration so shipped units upgrade cleanly. |
| Time | **NTP/RTC configured per unit, local source by default** *(revised — was `pool.ntp.org`)* | `controller_ts` drives all metrics (reference Rule 4). **Amended (AR-04):** the default NTP source is inside the OT boundary (broker host, switch, or local GPS/PTP appliance), not `pool.ntp.org`, since an isolated segment can't reach the internet. Interval/duration math uses a monotonic clock; the wall clock is for stamping only. A backward wall-clock step is logged as an event, never allowed to silently reorder history. |
| Rule engine scope | **Monitoring + non-protective actuation only** *(new, AR-01)* | The rule engine may drive indicators, andon, and non-safety convenience outputs. An output-class allow-list in the schema rejects anything outside that at validation time. Any output whose failure could injure someone or damage equipment must be hardwired or run on a SIL-rated safety PLC, fully independent of this device. |
| Output fail-safe | **Hardware watchdog required on every output + the board** *(new, AR-02)* | Every output module must de-energize (or move to a documented safe state) on loss of heartbeat from the poll engine — Variant B's amxmotion "Bus Error Reset" behavior becomes a hardware requirement for Variant A too, not just something Variant B happens to get. The board itself carries a hardware watchdog that resets it if the poll loop stops petting it. Default behavior is de-energize on comms loss; "hold" is an explicit, documented per-output commissioning choice. |
| Control authority | **Exactly one owner per physical output** *(new, AR-03)* | Each output is declared `edge`-owned or `plc`-owned in the device map. The rule engine can read a PLC-owned point but never write it; the UI blocks and warns if a rule targets one. Removes the two-masters-one-actuator hazard where an existing machine PLC and the edge rule engine could both command the same coil. |
| Key storage | **Secure element / TPM for broker credentials** *(new, AR-06)* | The MQTT client private key is provisioned into hardware (e.g. an ATECC608-class secure element, or a CM4 platform with TPM) so it never exists in an extractable file on the SD card. Credentials are per-device, so one compromised unit can't impersonate the fleet, and certificates are revocable. |
| Actuating rule changes | **Permit-to-edit gate for anything touching an output** *(new, AR-07)* | Non-actuating config (naming, scaling, telemetry points) keeps instant hot-reload. A rule change affecting an output requires explicit operator acknowledgement of the resulting output states, or a "line stopped / permit-to-edit" mode, before it takes effect. |
| RTU degradation | **Per-slave timeout/retry + multi-rate scan for RS485** *(new, AR-08)* | RTU gets the same per-device timeout/retry/mark-dead fields TCP already has. A silent slave is marked dead and re-probed at a slow rate instead of stalling the full cycle every time; per-device health (last-seen, error rate) is reported to the UI and event stream. |
| Hardware platform | **Consumer Pi + SD acceptable for pilots only** *(new, AR-10)* | Explicitly documented as a pilot-grade limitation, not a shipped-product claim — poor MTBF in a hot/noisy cabinet, no default watchdog beyond what AR-02 adds, SD wear remains the top failure mode. Revisit before any warrantied shipment. |
| IIoT state model | **Custom scheme for now; Sparkplug B evaluation scheduled** *(new, AR-11)* | The bespoke MQTT topic/`boot_id` scheme stays as-is short term, but a build-vs-adopt evaluation against MQTT Sparkplug B is on the roadmap before the fleet grows large enough that switching becomes expensive. |

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
    },
    {
      "id": "rule_purge_valve",
      "enabled": true,
      "match": "all",
      "when": [{ "point": "purge_start", "op": "rising" }],
      "then": [
        // Normally-closed valve: pulsing means driving it LOW (open) for
        // ms, then reverting HIGH (closed) — the logical opposite of
        // whatever "value" was. Omitting "value" defaults to true, which
        // is the normally-open case (rule_button_led above).
        { "action": "pulse", "point": "nc_purge_valve", "ms": 800, "value": false }
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
- **`pulse` has a direction: `value` (boolean, default `true`) is the ACTIVE state it pulses TO; the revert after `ms` is always the logical opposite.** Default `true` covers a normally-open output (drive HIGH briefly, settle LOW — `rule_button_led` above). Normally-closed outputs (drive LOW briefly, settle HIGH — `rule_purge_valve` above) set `"value": false`. This needed no schema change: `value` was already a generic action-object field shared with `set`; only the rule-engine interpreter's hardcoded `True` needed to become `action.get("value", True)`, with the revert computed as `not active_value` instead of a hardcoded `False`.
- **`when` is an array with `match`, even though v1 allows only one condition.** The evaluator loops the array and applies `match` (`all`=AND / `any`=OR). v1 validation caps the array at length 1; lifting that cap plus adding timer/counter operators is the *only* change needed for multi-condition logic later — no schema migration.
- **Analog is reserved, not wired.** `scaling`/`unit` stay in the point schema and the `analog_in` kind is defined, but v1 validation rejects `analog_in` points. Enabling analog later is a UI toggle + removing that validation guard, not a data-model change.
- **Every save is attributed.** `updated_by` carries the username typed at login (audit only). Access is gated by two passwords: **operator** (IO/rules) and **admin** (identity, network, bus transport, factory reset, OTA). Destructive actions require the admin password even within a session.
- **`config_version` drives hot reload and rollback.** Last-known-good (LKG) config is retained so a bad save can auto-revert.
- **`schema_version` drives OTA migration.** It is the *product* schema version, distinct from the per-save `config_version`. When an OTA update ships a newer schema, a migration step upgrades the on-device config from its `schema_version` to the new one before the new app runs. Migrations are forward-only, tested, and backed up first.
- **Identity edits emit an `identity_change` event.** Changing plant/line/zone/station is allowed (admin + confirm) but is logged as an event to the server so the history fork is explicit and traceable — never silent. `boot_id` is never touched here.
- **Transport is a config choice: RS485 (Modbus RTU) or Modbus TCP.** `pymodbus` provides both `ModbusSerialClient` and `ModbusTcpClient`, so the poll engine picks the client at load time from `bus.transport`. Everything above the client — point map, scaling, rules, hot reload — is transport-agnostic. `unit_id` is the RS485 slave address for RTU and the unit/slave id for TCP (usually `1` for a native TCP IO module).
- **TCP topology (resolved):** the edge Pi is the **Modbus TCP client**; each IO module is its **own TCP server with its own IP**. So in TCP mode **`tcp.host` lives on the device, not the bus** — one `ModbusTcpClient` connection per module IP, pooled and reused. Method A (Pi → switch → modules) and Method B (Pi → wireless gateway → modules) are identical at the Modbus layer; they differ only in link reliability. **Method B needs a longer `timeout_ms` and a retry/backoff policy** because wireless drops and jitter are normal — the poll engine treats a timed-out read as "stale", not "zero", so a Wi-Fi hiccup never fakes a sensor value or mis-fires a rule.
- **Bus Scan is transport-aware:** RTU scans unit ids 1–32 on the serial line; TCP pings a user-supplied IP range (or list) on port 502 and reports which modules answer.
- **Output class and ownership are now first-class point fields (AR-01, AR-03).** Every `digital_out`/`register`-write point carries `output_class` (`"indicator"` | `"non_safety_actuation"` — anything else is rejected at validation) and `owner` (`"edge"` | `"plc"`). The rule engine may only write points where `owner == "edge"`; a rule targeting a `plc`-owned point fails validation with a clear error, and the UI warns before that state is even reached. Existing example rules that drive process-critical outputs (a weld-active relay, a vent valve) are the reason this field exists — they must be re-homed onto a real PLC/safety controller, not the edge rule engine, once this lands.
- **Actuating changes are permit-gated (AR-07).** A config save is classified by whether it touches any `owner: "edge"` output's rule wiring. Non-actuating saves (names, scaling, telemetry-only points) hot-reload exactly as before. Actuating saves require an explicit acknowledgement step showing the resulting output states before they take effect.

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

---

## Build status (post Phase 8)

Phases 0–8 are built and tested in `io_config_system/` (137 tests). What
that does and doesn't prove is not restated here — see
`io_config_system/docs/VALIDATION_MATRIX.md` for the full traceability
matrix (every risk in the table above, every reference-doc checklist item,
mapped to a specific test or flagged as unprovable without real hardware),
and `io_config_system/docs/INSTALLER_GUIDE.md` for the commissioning
walkthrough (executed for real, end to end, by
`tests/test_installer_guide.py`).

Known gaps carried forward, not silently dropped:

- `POST /api/factory-reset` — documented in `api_contract.md`, never implemented.
- The sync agent (SQLite → MQTT, QoS 1, ACK-only synced flag) — out of scope for this project; reference §8 still needed, unchanged.
- OTA's signature/migration/rollback machinery is real and tested; the signed binary app package, staged download, and device-reboot health check it would run against in production are not — no Pi to test them on.
- `identity.py`'s first-boot `boot_id` generation path is ported but not exercised by an automated test in this repo.
- The reference script's own 300ms debounce code doesn't do what its comments say (see `engine/debounce.py`) — this build implements the debounce the comments describe, not the code's actual behavior. Flag before sign-off if bit-for-bit parity with the shipped bug is somehow the requirement.

---

## Deployment Variant B — Multi-Zone (Windows IPC, wired switch and/or wireless)

Everything above (Phases 0–8) is **Deployment Variant A**: one Linux/Pi-class terminal per station, RS485 or Modbus TCP, one identity, one event log. That variant is built, tested, and unchanged by what follows.

For larger sites, a second variant was scoped: **one Windows IPC overseeing several physically separate zones** (e.g. a robot cell, a welding jig, a leak-test rig) instead of one terminal per station. This is an **additional** variant, not a replacement — both must be built and maintained going forward.

### Topology

Two link methods reach each zone's RTU (a PLC acting as its own Modbus TCP server), and **a real site can mix them zone-by-zone** on one terminal:

- **Method A — wired.** Main Terminal → Ethernet switch → Zone RTU(s) → field IO (robot I/O, operator box, alarm, welding-jig I/O, leak-test I/O). Simplest and most reliable; a wired zone's switch is a single point of failure for everything behind it (site should keep a spare).
- **Method B — wireless.** Main Terminal → a per-zone Waveshare RS485-to-WiFi/ETH gateway → Zone RTU → field IO. Vendor-stated best-case WiFi range is **~50 m in open air** — a real on-site RF survey is required before finalizing gateway placement; expect that range to be worse on an actual factory floor with steel and RF noise.

At the Modbus layer both methods are identical — the engine always speaks plain Modbus TCP and does not know or care which physical link carried the bytes. Method A needs no new poll-engine code; the differences are all in configuration defaults and commissioning UI, captured below.

### Decisions locked in for this variant

| Decision | Choice | Consequence |
|---|---|---|
| Terminal OS | **Windows IPC, fully owned by us** — not the customer's IT/domain | We control imaging, patch policy, and update windows; Windows Update reboots are scheduled on our terms, not fought against |
| Scope | **Additional variant**, not a replacement for Deployment Variant A | Both the Linux single-terminal build and this Windows multi-zone build are maintained in parallel |
| Identity | **Each zone keeps its own independent identity** (plant/line/zone/station + event log) — not one shared station identity with a zone tag | Requires a **multi-zone orchestrator**: N independent `PollEngine` instances (one per zone, each with its own `ctrl_id.json`/`system_config.json`/`io_config.json`/event DB) running inside one Windows Service process, rather than a single engine watching multiple devices |
| Fail-safe on comms loss | **Off on dropout is correct, as shipped** | The amxmotion ETH-MODBUS-IO16R's factory-default "Bus Error Reset" (de-energizes all relay outputs ~1 s after a Modbus TCP link drops) is the desired behavior — deliberately left alone, not reconfigured to "Bus Error Hold" |
| Link medium | **Per-zone, not per-site** — `link.medium` (`"wired"` \| `"wireless"`) added to each zone's `io_config` | Descriptive only to the engine, but drives commissioning defaults: wired = 800 ms timeout / 2×200 ms retries / 100 ms poll; wireless = 1500 ms timeout / 3×400 ms retries / 150 ms poll. Also switches which warning banner the commissioning UI shows |
| Network security | **Same posture as Variant A — VLAN isolation is mandatory for BOTH methods** | Modbus TCP has no authentication regardless of transport; a wired switch is not inherently safer than a wireless bridge just because there's no radio to eavesdrop on |
| Zone RTU hardware | New equipment for this variant — register map/addressing is ours to design, not a brownfield constraint | Clean, non-overlapping Modbus addressing can be designed per zone from scratch |

### What this adds to the engineering plan (built — see docs/MULTI_ZONE_GUIDE.md)

1. **Multi-zone orchestrator** — `engine/zone_orchestrator.py`'s `ZoneOrchestrator` runs N independent `PollEngine`/`RuleEngine` instances (the existing, tested Variant-A code, reused unmodified per zone), each supervised on its own thread; a crashing zone is caught, logged, and restarted with backoff without affecting any other zone. `engine/zone_loader.py` loads each zone from its own `ctrl_id.json`/`system_config.json`/`io_config.json`/`event_log.db` directory. Tested in `tests/test_zone_orchestrator.py` and `tests/test_zone_loader.py` (fault isolation, restart-with-backoff, crash logging, directory discovery).
2. **Windows-specific plumbing** — `service/windows_service.py` wraps `ZoneOrchestrator` as a Windows Service (`SvcDoRun`/`SvcStop`). Genuinely untested on real Windows/pywin32 (no such machine in the build environment) — the module guards its `pywin32` import so it still imports cleanly on Linux/macOS, and everything underneath it (`build_orchestrator`, the loader, the orchestrator itself) is fully tested cross-platform. COM-port device paths need no code change (`bus.serial.port` is just a string pymodbus passes through). Windows Update/maintenance-window policy remains a deployment-time Group-Policy decision, not code.
3. **Zone-scoped Flask routes** — `api/multi_zone_app.py`'s `create_multi_zone_app()`: `/api/zone/<id>/io`, `/live`, `/bus/scan`, `/test/write`, `/identity`, `/system`, `/commissioning-mode`, `/config/versions`, `/config/rollback`, `/io/export`, `/io/import`, plus `/api/zones` (fleet view). One shared Flask process, one shared login, N zone-addressed engines; an unknown `zone_id` is a clean 404. Tested in `tests/test_multi_zone_app.py`, including that AR-07's permit-to-edit gate carries over per zone unchanged.
4. **`link.medium` field** — added to `io_v2.schema.json` as an optional per-zone `link: {medium: "wired"|"wireless"}`; purely descriptive to the engine. `engine/link_medium.py`'s `recommended_comms_defaults()` hands back the commissioning defaults (wired = 800 ms timeout / 2×200 ms retries / 100 ms poll; wireless = 1500 ms timeout / 3×400 ms retries / 150 ms poll) for a commissioning UI to pre-fill. Tested in `tests/test_link_medium.py`. The commissioning-UI selector itself is prototyped in the mockup (see below) but not yet wired to a real frontend build.
5. **Mixed-fleet regression test** — `tests/test_multi_zone_app.py::test_mixed_fleet_wired_and_wireless_zone_settings_never_bleed_into_each_other`: one zone on wired-fast defaults and one on wireless-loose defaults, driven together inside the orchestrator, confirming the two independent engine instances' settings never bleed into each other.
6. **Commissioning checklist addition** — done in `docs/INSTALLER_GUIDE.md`'s "What this guide does NOT cover" section: VLAN/isolation applies to Method A too, not just Method B, with an explicit line to that effect; `docs/MULTI_ZONE_GUIDE.md` restates it per-zone.

**Not yet built**: zone-scoped OTA routes (Phase 7's `/api/ota/*` remains single-terminal only), and no field test against real RTU/TCP hardware — the mixed-fleet regression and all orchestrator tests run against fake Modbus clients.

---

## UI / Design Language

**Canonical design language:** `Design_Language_Apple.html`. Apple Human-Interface-style — system colors (blue=action, green=healthy, orange=caution, red=fault), translucent "glass" materials for chrome (sidebar/toolbar) over solid surfaces for content, the system font stack (`-apple-system`, falls back to Segoe UI on Windows — a real consideration since commissioning engineers may be on Windows laptops, not Macs), full light/dark mode. This was chosen over Material Design 3 (rejected — read as "management dashboard" rather than "precision instrument" for an audience of engineers/SIs plus C-level glances) and over the original dark SCADA look. **All new screens should reuse this file's tokens rather than introduce a new visual style.**

**Current reference mockup:** `Field_Terminal_Configurator.html`. Merges the original Phase 1 mockup's full feature set — two-tier login, Bus Settings, Devices & IO Points, Bus Scan, Live Values, Logic Rules, Apply & Rollback, admin-gated Identity/Network/Updates — with the multi-zone fleet overview and per-zone sidebar navigation from Deployment Variant B, restyled entirely in the Apple design language, including the `link.medium` selector. **This is the file to build the real app's UI from.**

**Superseded / reference-only, not the build target:**
- `io_config_mockup.html` — original Phase 1 mockup, dark SCADA look, single-terminal only. Superseded by `Field_Terminal_Configurator.html`.
- `Multi_Zone_Terminal_Mockup.html` — early fleet-overview-only pass (no bus settings/points/rules editors). Superseded.
- `Design_Language_Material.html` — Material Design 3 exploration. Rejected direction, kept for reference.

---

## File map (for a reader with no prior context)

| File | What it is | Status |
|---|---|---|
| `IO_Config_Execution_Plan.md` | This document — the master plan | Living document |
| `factory_iot_reference.md` | Original hardcoded `modbus_poll.py` reference behavior | Read-only, never modified |
| `io_config_system/` | All Phases 0–8 source code, schemas, tests (137 tests), docs | Built, tested, Deployment Variant A |
| `io_config_system/docs/VALIDATION_MATRIX.md` | Full risk/checklist traceability matrix for Variant A | Current |
| `io_config_system/docs/INSTALLER_GUIDE.md` | Commissioning walkthrough, executed for real by `tests/test_installer_guide.py` | Current |
| `io_config_system/api_contract.md` | Hand-written REST contract (Phase 0) | Slightly drifted from actual `api/app.py`; the installer guide/tests are the more accurate source |
| `Design_Language_Apple.html` | Canonical UI design language + living style guide | **Current — build from this** |
| `Field_Terminal_Configurator.html` | Full merged multi-zone configurator mockup | **Current reference mockup — build from this** |
| `io_config_mockup.html` | Original single-terminal mockup, dark SCADA style | Superseded |
| `Multi_Zone_Terminal_Mockup.html` | Early fleet-overview-only mockup | Superseded |
| `Design_Language_Material.html` | Material Design 3 exploration | Rejected direction, kept for reference |
| `Field_IO_Terminal_Architecture.html` | Interactive architecture diagram (customer-facing) | Reference/presentation, not a build target |
| `Architecture_Review.md` | External adversarial architecture/security/safety review (IEC 62443, ISA-95, IEC 61508/61511) | Reviewed; remediation decisions below |

---

## Architecture Review remediation (v1)

Reviewed against `Architecture_Review.md`. Every finding was decided individually, not batch-approved — this table is the record of what was decided and why, so the reasoning survives even if the review document doesn't get re-read. **None of this is implemented in `io_config_system/` yet** — these are plan-level decisions; the corresponding code/schema/hardware work is new scope, not yet scheduled into a phase number.

| AR ID | Finding (severity) | Decision | What actually changes |
|---|---|---|---|
| AR-01 | Control + supervision merged on one non-deterministic host (Critical) | **Restrict rule engine to monitoring + non-protective actuation.** | New `output_class` field + validation allow-list (see Design notes above). Existing weld-active/vent-valve-style rules must move to a real PLC/safety controller. |
| AR-02 | No hardware fail-safe on process failure, Variant A (Critical) | **Require a hardware watchdog on every output + the board.** | Variant A hardware spec gains a comms-loss-failsafe requirement (mirrors Variant B's amxmotion behavior) plus a board-level watchdog. Hardware/BOM change, not pure software. |
| AR-03 | Split-brain control authority, PLC vs edge (High) | **Automated single-owner-per-output check.** | New `owner` field (`edge`/`plc`) per output point; validator blocks edge rules from writing PLC-owned points (see Design notes above). |
| AR-04 | NTP default contradicts isolation posture; time-integrity gaps (High) | **Local time source by default; monotonic clock for intervals.** | `system_config` NTP default changes from `pool.ntp.org` to an in-boundary source; backward wall-clock steps are logged, never silently applied to event ordering. |
| AR-05 | Auth/audit below 62443 baseline (High) | **Real per-user accounts + TLS + lockout.** | Replaces the two-shared-password model; audit is bound to an authenticated account, not a typed name. Reverses a previously "resolved" decision — see amended Auth row above. |
| AR-06 | TLS private keys in clear on removable media (High) | **Secure-element/TPM key storage.** | MQTT client key is provisioned into hardware, never sits in an extractable file; per-device credentials, revocable. |
| AR-07 | Live hot-reload of control logic on a running line (Medium) | **Permit-to-edit gate for actuating changes.** | Non-actuating config keeps instant hot-reload; anything touching an `owner: "edge"` output's rule wiring requires acknowledgement before it takes effect (see Design notes above). |
| AR-08 | RTU polling doesn't degrade gracefully (Medium) | **Per-slave timeout/retry + multi-rate scan for RTU.** | RTU gains the timeout/retry/mark-dead fields TCP already has; dead slaves re-probed slowly instead of stalling every cycle. |
| AR-09 | No central config backup / fleet drift control (Medium) | **Add central backup + drift reporting.** | Amends the "no central dependency" decision — see amended row above. New central-side component; the device still works standalone if the server is unreachable. |
| AR-10 | Consumer hardware for a shippable product (Medium) | **Keep consumer Pi for now — pilot-only, documented.** | No hardware change yet. Explicitly labeled pilot-grade, not a shipped-product claim, in contrast to AR-02's watchdog requirement which does land now. |
| AR-11 | Custom MQTT state model reinvents Sparkplug B (Low) | **Schedule a build-vs-adopt evaluation.** | No change today; added to the roadmap as a decision point before the fleet grows large enough that switching becomes expensive. |

**Sequencing note:** AR-01/02/03 (Critical/High, safety- and authority-adjacent) block any safety-relevant deployment. AR-04/05/06 block selling into a customer that runs a security review. AR-07/08/09/10/11 are fleet-scale hardening — real, but they don't block a single-terminal pilot. This mirrors `Architecture_Review.md` §5's own prioritization; nothing here reordered it.
