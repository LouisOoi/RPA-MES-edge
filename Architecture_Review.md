# Architecture Review — Field IO Terminal Configuration System

**Review v1 · July 2026**
Subject: the system described in `IO_Config_Execution_Plan.md` (Deployment Variants A and B) and `factory_iot_reference.md`.
Reviewed against: **IEC 62443** (industrial cybersecurity), **ISA-95 / Purdue Enterprise Reference Model** (control-system layering), and **IEC 61508 / 61511** (functional safety).
Audience: engineering team and system-integrator partners; suitable for customer security/safety review.

---

## 1. How to read this document

This is an adversarial review. It assumes the design is competent (it is — see §6) and spends its effort on where the architecture diverges from standard OT practice and where those divergences can hurt someone or lose data. Findings are ranked by severity, not by order of discovery.

### Severity model

Severity = Impact × Likelihood, with a safety override.

| | Impact: Low | Impact: Medium | Impact: High | Impact: Safety* |
|---|---|---|---|---|
| **Likelihood: High** | Medium | High | Critical | Critical |
| **Likelihood: Medium** | Low | Medium | High | Critical |
| **Likelihood: Low** | Low | Low | Medium | High |

\* *Safety impact = a plausible path to injury or equipment damage. Any credible safety path is at least High regardless of likelihood, because the acceptable frequency of a safety failure is near zero.*

Each finding carries a **confidence** tag — `[Certain]` (verifiable from the documents), `[Likely]` (strong inference), `[Guessing]` (gap-filling, stated as such).

---

## 2. Executive summary

The system is well-engineered *as a monitoring and configuration platform*. Its data-integrity design (compound identity, `boot_id`, strict `controller_ts` discipline) is above average for an edge-of-network product. The problems are not in the data path; they are in the **decision to let this device perform control**, and in a set of **security and availability gaps** that standard OT review will flag.

The single most important finding is architectural, not a bug: the terminal blurs the ISA-95 boundary between control (Level 1) and supervision (Level 2) by running browser-editable actuation logic on a non-deterministic Linux host. Everything protective must be moved out of that path. The rest are hardening items — real, but addressable without redesign.

### Findings register

| ID | Finding | Severity | Primary standard |
|---|---|---|---|
| AR-01 | Control and supervision merged on one non-deterministic host | **Critical** | ISA-95 L1/L2; IEC 61508 |
| AR-02 | No hardware fail-safe on edge process failure (Variant A) | **Critical** | IEC 61508; IEC 62443 FR7 |
| AR-03 | Split-brain control authority (existing PLC vs edge rule engine) | **High** | ISA-95; IEC 61511 |
| AR-04 | NTP source contradicts network-isolation posture; time-integrity gaps | **High** | IEC 62443 FR6/FR7 |
| AR-05 | Config-app authentication and audit below 62443 baseline | **High** | IEC 62443 FR1/FR2 |
| AR-06 | TLS private keys stored in clear on removable media | **High** | IEC 62443 FR3/FR4 |
| AR-07 | Live hot-reload of control logic on a running line | **Medium** | ISA-95 change control |
| AR-08 | RTU polling does not degrade gracefully | **Medium** | IEC 62443 FR7 |
| AR-09 | Local-only config: no central backup / drift control for a fleet | **Medium** | IEC 62443 FR7; ops |
| AR-10 | Consumer hardware for a shippable industrial product | **Medium** | Environmental / reliability |
| AR-11 | Custom MQTT state model reinvents an IIoT standard (Sparkplug B) | **Low** | IIoT convention |

---

## 3. Standards context (one paragraph each)

**ISA-95 / Purdue.** The reference model layers a plant: Level 0 field devices (sensors, actuators), Level 1 basic control (PLCs, safety controllers — deterministic, real-time), Level 2 supervisory control (SCADA/HMI — soft real-time), Level 3 MES, Level 4 enterprise. The load-bearing idea is that *control* lives at L1 on deterministic hardware and *supervision/data* lives at L2+. Mixing them removes the guarantees each layer is supposed to provide.

**IEC 62443.** The OT-security standard family. Its seven Foundational Requirements are FR1 Identification & Authentication Control, FR2 Use Control, FR3 System Integrity, FR4 Data Confidentiality, FR5 Restricted Data Flow, FR6 Timely Response to Events, FR7 Resource Availability. Components and systems are certified to Security Levels (SL 1–4). A shipped product sold into industrial customers will be assessed against these.

**IEC 61508 / 61511.** Functional safety. Any function whose failure can cause harm must be implemented to a Safety Integrity Level (SIL) with quantified failure rates, systematic-capability requirements, and independence from non-safety functions. Safety functions in general-purpose software on general-purpose OS/hardware cannot claim a SIL without enormous justification — the standard's default expectation is a rated safety controller or hardwired logic.

---

## 4. Detailed findings

### AR-01 — Control and supervision are merged on one non-deterministic host `[Certain]` · Critical

**What the design does.** The rule engine reads a point and writes a relay/coil (`overtemp → output`, `button → coil`, `purge_start → valve`). This is closed-loop control executing in Python, on Linux, inside a ~100 ms poll loop, with the control logic authored by an operator through a web browser.

**Why it violates standard practice.** ISA-95 places control at Level 1 on deterministic hardware; this device is a Level-2 supervisory/data node doing Level-1 work. Linux scheduling jitter, the Python GIL, garbage-collection pauses, and SD-card I/O stalls mean the actuation-latency distribution has a long tail measured in hundreds of milliseconds to seconds — with no upper bound the design can prove. For any output that matters, that is not control; it is best-effort.

**Impact.** If any browser-editable rule drives a protective or process-critical output, a runtime stall produces a late or missed actuation. Safety-class impact.

**Remediation.**
- *Immediately (policy):* declare in writing that the terminal performs **monitoring plus non-protective actuation only** — indicators, andon, non-safety solenoids/convenience outputs. Enumerate an allow-list of output classes the rule engine may drive; validation rejects anything outside it.
- *Design:* keep all protective interlocks hardwired or in a SIL-rated safety PLC at Level 1, fully independent of this device (IEC 61511 separation of safety and control).
- *Product:* label the product's control capability honestly in the datasheet ("supervisory actuation, non-safety") so no integrator designs a safety function onto it.

---

### AR-02 — No hardware fail-safe on edge process failure (Variant A) `[Certain]` · Critical

**What the design does.** In Variant A (RS485-direct), if the poll-engine process hangs — deadlock, I/O stall, OOM — the last coil states persist. An energized relay stays energized indefinitely. There is software safe-state *during config reload*, but nothing covers a process hang or crash.

**Contrast.** Variant B gets this right by accident of hardware: the amxmotion ETH-MODBUS-IO16R's Bus Error Reset de-energizes outputs ~1 s after the Modbus link drops. Variant A has no equivalent dead-man.

**Why it matters.** IEC 61508 requires outputs to move to a defined safe state on controller failure (fail-safe). A controller that fails with outputs held is fail-to-danger.

**Remediation.**
- Require every output module to have a **hardware watchdog / comms-loss failsafe** that de-energizes (or moves to a defined safe state) when it stops receiving a heartbeat from the poll engine — i.e. adopt the Variant-B module behaviour as a Variant-A hardware requirement, not a software feature.
- Add a hardware watchdog on the Pi that resets the board if the poll loop stops petting it.
- Make "de-energize vs hold on comms loss" a documented, per-output commissioning decision, defaulting to de-energize.

---

### AR-03 — Split-brain control authority `[Likely]` · High

**What the design does.** The bus already carries a "Machine PLC" as a Modbus slave running its own program and driving its own outputs. The edge rule engine can now also write coils. Two independent controllers can command the same physical IO. The plan's output-contention check compares only rules *within* the edge config; it cannot see the PLC.

**Why it matters.** Undefined control authority is a classic integration hazard: two masters, one actuator, unpredictable result. ISA-95 and IEC 61511 both assume a single, defined owner per actuation.

**Remediation.**
- Define, per physical output, **exactly one owner** (PLC or edge). Document it.
- Make the terminal **read-only** on any point the PLC controls; the rule engine may observe PLC-owned points but not write them.
- Add a commissioning-time check: an output claimed by an edge rule must be declared "edge-owned" in the device map, and the UI must warn when a rule targets a PLC-owned point.

---

### AR-04 — NTP source contradicts isolation posture; time-integrity gaps `[Certain]` · High

**What the design does.** The security posture mandates "no route to the office network or the internet." The time config defaults to `ntp: ["pool.ntp.org"]`, which requires internet. On an isolated OT VLAN this silently never syncs.

**Why it matters.** All metrics depend on `controller_ts` (reference Rule 4). Two failure modes: (1) the clock never sets on an isolated network and free-runs from the RTC, drifting; (2) when NTP *is* reachable, a step correction can move the wall clock backward, breaking event ordering and any interval computed from timestamps — exactly the data the whole system exists to produce.

**Remediation.**
- Provide time from **inside** the OT boundary: a local NTP server (the broker host, a switch, or a small GPS/PTP appliance). Never rely on `pool.ntp.org` on an isolated segment; change the default.
- Use a **monotonic clock** for all interval/duration math; reserve the wall clock for stamping only.
- Treat backward wall-clock steps as a logged event; never let a step silently reorder events.
- Document RTC drift spec and a resync cadence.

---

### AR-05 — Config-app authentication and audit below the 62443 baseline `[Likely]` · High

**What the design does.** Two shared passwords (operator/admin), no per-user accounts, no lockout, and the config UI appears to serve plain HTTP on :8080. The audit trail is a **username the user types at login** — unauthenticated self-declaration.

**Why it matters.** IEC 62443 FR1 (Identification & Authentication Control) and FR2 (Use Control) expect uniquely identified users, authenticated sessions, and audit tied to that identity. A typed name is not attribution; anyone can enter any name. A customer security team will reject this for anything beyond SL 1, and plaintext HTTP on the LAN fails confidentiality of the session and credentials.

**Remediation.**
- Serve the config UI over **TLS** (self-signed with a documented trust step is acceptable on an isolated segment).
- Real **per-user accounts** with role assignment (operator/admin as roles, not shared passwords), plus lockout/backoff on failed attempts.
- Bind the audit log to the **authenticated** user, not a typed string.
- Consider optional integration with the customer's identity provider for larger deployments.

---

### AR-06 — TLS private keys stored in clear on removable media `[Certain]` · High

**What the design does.** `/etc/certs/client.key` (and the broker CA/client certs) live on an unencrypted SD card.

**Why it matters.** Anyone with physical access to the cabinet can remove the card and extract the broker client key, then impersonate the terminal to the broker (publish forged events, subscribe to commands). IEC 62443 FR3 (System Integrity) and FR4 (Data Confidentiality) expect credential protection commensurate with physical exposure — and a field cabinet is physically exposed.

**Remediation.**
- Store keys in a **hardware secure element / TPM** (e.g. an ATECC608-class chip or CM4 with TPM) so the private key never leaves hardware.
- If secure-element hardware is not in v1, **encrypt the key store** and document that physical access equals compromise as a known, accepted limitation.
- Provision per-device credentials so extracting one device's key cannot impersonate the fleet, and support certificate revocation.

---

### AR-07 — Live hot-reload of control logic on a running line `[Likely]` · Medium

**What the design does.** Config (including rules that drive outputs) hot-reloads with "no restart, no missed cycles," with per-point safe-state during the swap window.

**Why it matters.** Hot-swapping *control* logic while outputs are live is contrary to standard commissioning practice, which changes control logic with the machine in a known/stopped state. The "no relay glitch" guarantee is hard to prove on Linux, and an operator editing rules on a running line can produce an unexpected actuation on save. (This is less of an issue once AR-01 is resolved and the device drives only non-protective outputs — the two findings are linked.)

**Remediation.**
- For any output above the "indicator" class, require an explicit **operator acknowledgement of resulting output states** before a rule change takes effect, or gate rule changes behind a "line stopped / permit-to-edit" mode.
- Keep hot-reload for **non-actuating** config (naming, scaling, telemetry points) where it is genuinely safe and useful.

---

### AR-08 — RTU polling does not degrade gracefully `[Likely]` · Medium

**What the design does.** Only the TCP transport carries timeout/retries/backoff in the schema. RTU has none, and the poll model is a flat cycle across all points.

**Why it matters.** On RS485, one silent slave stalls the whole scan waiting for a timeout, every cycle — and with up to 32 slaves the cycle time collapses. This is an availability issue (FR7) and it directly undermines the 100 ms poll assumption the rest of the design rests on.

**Remediation.**
- Add **per-slave timeout, retry, and mark-dead** logic for RTU (mirror the TCP fields).
- Implement **multi-rate scanning**: critical points fast, telemetry slow; dead devices polled at a slow re-probe rate so they don't tax the hot loop.
- Report per-device health (last-seen, error rate) to the UI and to the event stream.

---

### AR-09 — Local-only config has no central backup or drift control `[Certain]` · Medium

**What the design does.** Config lives on the Pi; portability is manual export/import. The OTA channel pushes firmware down but there is no config backup *up*.

**Why it matters.** For a fleet product this is an operations gap: an SD failure loses the site's configuration unless someone remembered to export it; there is no central inventory, no fleet-wide config audit, and no drift detection between "as-designed" and "as-running." The reference itself calls SD wear the #1 Pi failure mode, which makes this concrete, not theoretical.

**Remediation.**
- On every successful config apply, **push a copy of the (non-secret) config to the server** over the existing MQTT/TLS channel; keep versioned backups centrally.
- Provide **restore-to-device** from the central copy as part of RMA/replacement.
- Add fleet-level drift reporting (which units differ from their intended config).

---

### AR-10 — Consumer hardware for a shippable industrial product `[Likely]` · Medium

**What the design does.** A consumer Raspberry Pi + SD card is the terminal.

**Why it matters.** Consumer Pi hardware in a hot, electrically noisy cabinet has poor MTBF, no default hardware watchdog, no surge/ESD protection, and RS485 isolation only if the HAT provides it. Acceptable for a pilot; weak as a shipped product with a warranty.

**Remediation.**
- Move to an **industrial Pi-class platform** (e.g. Revolution Pi, or a CM4 carrier with eMMC, hardware watchdog, wide-temp rating, isolated RS485, surge protection) or a purpose-built edge gateway.
- Specify the **operating environment** (temperature, vibration, EMC) in the datasheet and qualify against it.

---

### AR-11 — Custom MQTT state model reinvents an IIoT standard `[Guessing]` · Low

**What the design does.** A bespoke topic scheme plus `boot_id` handles device state, reboots, and event ordering.

**Why it matters.** **MQTT Sparkplug B** is the IIoT standard that already solves device birth/death/rebirth, state, and sequence integrity on top of MQTT. The custom scheme is not wrong, but it is maintenance you own forever and interoperability you don't get. Worth a conscious build-vs-adopt decision, not a silent default.

**Remediation.** Evaluate Sparkplug B (or at least document why the custom scheme was chosen over it) before the fleet grows and the scheme becomes load-bearing.

---

## 5. Prioritized remediation roadmap

**Before any safety-relevant deployment (do first):**
- AR-01 policy decision: monitoring + non-protective actuation only; output-class allow-list.
- AR-02 hardware failsafe / watchdog on every output and the board.
- AR-03 single-owner-per-output rule + read-only on PLC-owned points.

**Before selling into a security-reviewed customer:**
- AR-05 TLS + real accounts + authenticated audit.
- AR-06 secure key storage (or documented accepted limitation + per-device creds + revocation).
- AR-04 local time source + monotonic intervals.

**Before fleet scale:**
- AR-08 RTU graceful degradation + multi-rate scan.
- AR-09 central config backup + drift reporting.
- AR-10 industrial hardware qualification.
- AR-07 permit-to-edit for actuating rule changes.
- AR-11 Sparkplug B build-vs-adopt decision.

---

## 6. What the design gets right (keep these)

Credible strengths, above average for an edge product, that this review does not want to see regressed:

- **Compound identity + `boot_id`** — a correct, well-reasoned answer to the SD-wipe / AUTOINCREMENT-collision data-loss failure mode.
- **Strict `controller_ts` discipline** — using controller time (not server time) for all metrics is the right call and consistently applied.
- **Three-file blast-radius separation** (identity / system / IO) — an everyday IO edit cannot rewrite identity or network.
- **No-`eval` whitelisted rule engine** — the only safe way to accept logic from a browser; contention checks and stable point IDs are good hygiene.
- **Atomic writes + last-known-good + signed OTA with auto-rollback** — mature update/config-integrity practice.
- **VLAN isolation posture** (IEC 62443 FR5, Restricted Data Flow) — correctly identified as mandatory for both wired and wireless, which is the point many teams miss.

---

## 7. Standards mapping (quick reference)

| Finding | IEC 62443 FR | ISA-95 / Purdue | IEC 61508/61511 |
|---|---|---|---|
| AR-01 | — | L1/L2 boundary | Safety/non-safety separation |
| AR-02 | FR7 Availability | L1 | Fail-safe on failure |
| AR-03 | FR7 | Control authority | Single safety owner |
| AR-04 | FR6, FR7 | L2 data integrity | — |
| AR-05 | FR1, FR2 | — | — |
| AR-06 | FR3, FR4 | — | — |
| AR-07 | — | Change control | — |
| AR-08 | FR7 | L1/L2 | — |
| AR-09 | FR7 | Ops/L3 | — |
| AR-10 | — | Environmental | Systematic capability |
| AR-11 | — | Interoperability | — |

---

*Prepared as an internal/customer-facing architecture review. Findings reflect the documents reviewed (`IO_Config_Execution_Plan.md`, `factory_iot_reference.md`) as of July 2026; some rely on inferred implementation detail and are tagged `[Likely]`/`[Guessing]` accordingly — confirm against the actual `io_config_system/` source before treating any single item as final.*
