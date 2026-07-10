# Validation Matrix — Phase 8

Traces every risk, checklist item, and validation requirement named in
`IO_Config_Execution_Plan.md` and `factory_iot_reference.md` to the specific
test(s) in this repo that prove it, or states plainly that it isn't provable
without real Pi/hardware. No row in this document is a claim without a
pointer — if a row says "proven," you can go read the named test right now
and see it pass.

## 1. Plan risks table ("Risks & how the plan handles them")

| Risk | Mitigation (as designed) | Proof |
|---|---|---|
| Bad config energizes a relay unsafely | Whitelisted rule engine (no `eval`), per-point safe-state on reload, test writes gated behind commissioning mode with timeout | `test_rule_engine.py` (no unwhitelisted op/action ever executes — `_evaluate_condition`/`_run_actions` raise on anything not in the whitelist), `test_hot_reload.py::test_outputs_driven_to_safe_state_before_swap` + `test_custom_safe_state_is_honored`, `test_test_write.py::test_successful_write_auto_reverts_to_safe_state` + `test_disabling_commissioning_mode_does_not_cancel_pending_revert` |
| Half-written config file after crash | Atomic write (temp+rename) + JSON-Schema validation before swap + retained LKG | `test_hot_reload.py::test_atomic_write_never_leaves_a_half_file` (a genuinely interrupted write, not a mock), `test_invalid_reload_leaves_running_state_untouched` |
| Browser/HTTP fault stalls Modbus | Web app and poll engine are separate in-process objects sharing only the config file + a read-only snapshot | **Partially proven.** `PollEngine`/Flask separation is real in this code, and `test_api_phase6.py` exercises both together without one blocking the other in-process. What's NOT proven: this repo runs Flask and the poll loop in the same test process, not as genuinely separate OS processes — that's a deployment-topology property (`gunicorn`/systemd running two processes), not something a unit test proves. Flag this as a deployment checklist item, not a closed test. |
| Rule references a deleted point | Validation blocks the save; dangling references reported | `test_schemas.py::test_io_v2_rejects_dangling_point_ref` |
| Two rules fight over one coil | Output-contention check at validation time | `test_schemas.py::test_io_v2_rejects_output_contention` and `test_io_v2_allows_same_rule_writing_same_point_in_then_and_else` (the fix for the false-positive found while building Phase 6) |
| Two DIFFERENT points aliasing one coil | Not in the original risk table — found while building Phase 8's validation matrix and closed immediately | `test_schemas.py::test_io_v2_rejects_two_digital_out_points_on_same_coil` |
| `boot_id` edited/lost | Never UI/API-editable; lives in `ctrl_id.json` | `test_identity_store.py::test_boot_id_in_request_is_rejected_not_ignored`, `test_api_phase5.py::test_put_identity_boot_id_ignored_and_rejected` (rejected at the HTTP layer too, not just the store) |
| Identity changed → history silently forks | Admin-gated + explicit confirm + `identity_change` event | `test_identity_store.py::test_identity_change_event_logged_under_old_identity`, `test_api_phase5.py::test_put_identity_valid_change_and_event` |
| OTA update bricks a shipped unit | Signature verify, backup-first, health check, auto-rollback | `test_ota.py::test_failing_health_check_triggers_auto_rollback`, `test_api_phase7.py::test_ota_apply_forced_health_failure_rolls_back_via_http`. **Caveat, stated in `ota.py`'s own docstring:** there is no real signed binary app package or real device reboot to test a health check against — the crypto and the reload/rollback sequencing are real and tested; a real firmware image and a real health probe are not. |
| Wrong clock corrupts all metrics | NTP/RTC configured at commissioning | System config schema requires `time.ntp`/`timezone`/`rtc_present` (`schemas/system.schema.json`), commissioning flow sets it (`test_api_phase5.py::test_full_commissioning_flow`). **Not provable here:** that NTP sync actually occurs and `controller_ts` stays correct on real hardware — that's an on-device runtime property, not a config-validation one. |
| Everyday edit rewrites identity/network | Three separate config files; operator tier can't touch identity/system | `test_api_phase5.py::test_operator_forbidden_from_identity`, `test_api_phase5.py::test_system_requires_admin` |

## 2. Phase 8's own named validation matrix items

| Item | Proof |
|---|---|
| Address conflicts | `test_schemas.py::test_io_v2_rejects_two_digital_out_points_on_same_coil` (write conflicts flagged); `test_io_v2_allows_two_digital_in_points_reading_same_address` (read aliasing deliberately allowed — not a hazard) |
| Dangling rule refs | `test_schemas.py::test_io_v2_rejects_dangling_point_ref` |
| Output contention | `test_schemas.py::test_io_v2_rejects_output_contention` |
| Reload under load | `test_hot_reload.py::test_watcher_applies_valid_change_without_missing_a_poll`, `test_watcher_rejects_invalid_change_and_polling_continues` — a config change (good or bad) never costs the current cycle's actual IO poll |
| Offline edits | An "offline edit" is a file write to `io_config.json` that didn't go through the HTTP API. `ConfigWatcher` doesn't distinguish the source — `test_hot_reload.py::test_watcher_detects_version_bump` writes directly via `config_store.atomic_write_json`, exactly as an offline edit would, and the engine picks it up on the next cycle |
| Identity-change fork handling | `test_identity_store.py::test_identity_change_event_logged_under_old_identity`. **Not provable here:** the *server-side* gap-detection logic that treats a new identity as a fork rather than data loss lives outside this repo (reference §9); this repo's job ends at emitting the event honestly, which it does |

## 3. Reference doc's Phase 1 controller checklist — still passes on the new stack?

Reference (`factory_iot_reference.md` §14, "Phase 1 — Controller Foundation"),
checked against what this stack actually does instead of the original
hardcoded `modbus_poll.py`:

| Reference checklist item | Status on the new stack | Proof |
|---|---|---|
| RS485 HAT comms verified to every slave | Same physical requirement; config-driven instead of hardcoded | `engine/modbus_clients.py` builds the RTU client from `bus.serial`; `bus_scan.py::scan_rtu` is the verification tool. Real hardware still required to close this for real — no RS485 HAT exists in this sandbox |
| Slave addresses mapped (PLC fault regs, sensor IRs, I/O coils) | Mapping is now `points[]`, not constants | `test_migration.py::test_exit_criterion_seed_reproduces_hardcoded_behavior` pins the exact addresses from the original script into the seed config |
| `ctrl_id.json` created with 5 fields + `boot_id` at provisioning | Same file, admin-gated edits added on top | `schemas/identity.schema.json`, `engine/identity.py::load_identity`, `test_identity_store.py` |
| SQLite `event_log` v2 (6 identity columns) | Same schema | `engine/event_store.py::SCHEMA` — identical column set to reference §6 |
| `identity.py` reads/generates `boot_id` only if absent | Ported directly | `engine/identity.py` docstring explicitly restates reference Rule 2; no test currently exercises the "absent file" path in this repo's automated suite — **gap, listed below** |
| `log_event()` stamps all 6 fields on every row | Same helper, same guarantee | Every test that logs an event asserts all 6 identity fields (e.g. `test_poll_engine.py::test_bus_read_error_is_stale_not_zero_and_logs_event`) |
| `modbus_poll.py`: 100ms poll, 300ms SW debounce | Config-driven poll engine + a debounce implementation that's MORE correct than the original (see below) | `test_poll_engine.py` (poll mechanics), `test_rule_engine.py`/`test_hot_reload.py` (debounce) |
| Sync agent: compound identity in MQTT payload, QoS 1, ACK-only synced flag | **Not built in this repo.** `sync_agent.py` from the reference was never in scope for the io_config_system phases — this project replaces `modbus_poll.py` and adds the config/commissioning layer around it, not the MQTT sync agent | Not applicable to this repo; still needed from the reference implementation, unchanged |
| Offline test: RJ45 disconnected, polling continues, button logs to SQLite | Directly analogous behavior exists (poll loop doesn't depend on network) | Implied by architecture (poll engine and any future sync agent are separate concerns) but **not directly tested here** — no network-disconnect simulation exists in this suite |
| Reconnect test: events flush in order, gaps backfilled | Not applicable — sync agent out of scope here | N/A, see sync agent row |
| Reflash test: new `boot_id`, old server data untouched | `boot_id` generation logic ported; reflash-triggered regeneration lives in the (not-yet-built) factory-reset endpoint | `engine/identity.py` generates a fresh `boot_id` only when the file is absent, matching reference Rule 2 semantics, but the actual `/api/factory-reset` endpoint from `api_contract.md` is **not implemented** — listed as a known gap below |
| LED coil lights on button press | Directly reproduced, config-driven | `test_rule_engine.py::test_button_press_pulses_led_and_logs_maintenance_request` |

### Honest deviation, called out explicitly

The reference script's own debounce code doesn't implement 300ms debounce
despite saying so in its comments — see `engine/debounce.py`'s docstring.
This stack implements the debounce the reference's PROSE describes, not
the reference's actual buggy counter. If the customer specifically wants
bit-for-bit behavioral parity with the shipped script's bug, that's a
deliberate deviation to flag before sign-off, not an oversight.

## 4. Known gaps this matrix surfaces (not fixed as part of Phase 8)

- **No automated test exercises `identity.py`'s first-boot `boot_id` generation path.** It's a five-line function ported verbatim from the reference, but "ported verbatim" isn't the same claim as "tested here."
- **`POST /api/factory-reset` is documented in `api_contract.md` but never implemented.** It's the only code path that's allowed to generate a new `boot_id` post-provisioning, and it doesn't exist yet.
- **The sync agent (SQLite → MQTT, QoS 1, ACK-only synced flag) is entirely out of scope for this repo.** Every event this stack produces via `log_event()` is schema-compatible with the reference's sync agent, but nothing here actually ships events anywhere.
- **The Flask app + poll engine "separate processes" claim is architecturally true but only deployment-tested, not unit-tested**, per row 3 of the risks table above.
- **NTP/RTC actually keeping the clock correct is a runtime property of real hardware**, not something a config schema or a mock test can prove.
