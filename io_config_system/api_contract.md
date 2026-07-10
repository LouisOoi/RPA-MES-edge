# Field IO Terminal — REST Contract (Phase 0 draft)

Companion to `IO_Config_Execution_Plan.md`. Every terminal exposes this API on
`:8080`, bound to the OT interface only (per the Phase 6 security checklist).
No endpoint is reachable without a session cookie from `/api/login` except
`/api/login` and `/api/status` (health-check only, no config data).

## Auth tiers

| Tier | Can do |
|---|---|
| **operator** | `/api/io` (GET/PUT), `/api/live`, `/api/bus/scan`, `/api/test/write` |
| **admin** | Everything operator can, plus `/api/system`, `/api/identity`, `/api/factory-reset`, OTA endpoints |

Every write endpoint stamps `updated_by` on the resulting config from the
session's username — never trust a client-supplied `updated_by`.

## Endpoints

### `POST /api/login`
Body: `{ "username": str, "password": str }` → sets session cookie.
Response: `{ "tier": "operator" | "admin", "username": str }`.
`401` on bad credentials. No tier info leaked pre-auth.

### `GET /api/status`
No auth required. Health check only.
Response: `{ "schema_version": int, "config_version": int, "poll_engine": "running"|"stopped"|"degraded", "uptime_s": int }`.

### `GET /api/io`
Tier: operator. Returns the current validated `/etc/io_config.json` verbatim.

### `PUT /api/io`
Tier: operator. Body: full `io_config.json` document (schema_version 2).
Server behavior:
1. Validate structurally (`schemas/io_v2.schema.json`) and against business rules (`validators.validate_io`).
2. On failure: `422` with `{ "problems": [str, ...] }` (from `ConfigValidationError.problems`) — **running config is untouched**.
3. On success: bump `config_version`, stamp `updated_at`/`updated_by`, atomic write (temp+rename), retain previous as last-known-good, signal the poll engine to hot-reload (Phase 4).
Response: `{ "config_version": int }`.

### `GET /api/system`
Tier: admin. Returns `/etc/system_config.json`.

### `PUT /api/system`
Tier: admin. Body: full `system_config.json`. Validates against `schemas/system.schema.json`.
Applying network/NTP changes is delegated to a privileged helper process (Phase 5) — this endpoint only writes validated intent and queues the apply; it does not itself touch OS network state.
A `?test_only=true` query flag runs an MQTT "Test connection" without persisting (used by the UI's Test Connection button).

### `GET /api/identity`
Tier: admin. Returns `/etc/ctrl_id.json`. `boot_id` is included but flagged read-only in the response envelope: `{ "identity": {...}, "boot_id_editable": false }`.

### `PUT /api/identity`
Tier: admin. Body: `{ "plant_id", "line_id", "zone_id", "station_id", "confirm_breaks_continuity": true }`.
- **`boot_id` in the request body is ignored if present, never applied.** This is enforced in code, not just by convention — the handler must strip it before validation.
- Requires `confirm_breaks_continuity: true`; `400` without it.
- On success: writes new identity, emits an `identity_change` event to the server (reference Rule 2 / plan §"Identity edits").

### `GET /api/live`
Tier: operator. Read-only snapshot of current point values from the poll engine's shared live-values buffer (not the Modbus bus directly — never blocks on IO).
Response: `{ "points": { "<point_id>": { "value": ..., "stale": bool, "ts": int } } }`.

### `POST /api/bus/scan`
Tier: operator. Body: `{ "transport": "rtu" } | { "transport": "tcp", "ip_range": "192.168.10.10-192.168.10.20" }`.
RTU: scans unit ids 1–32 on the configured serial line. TCP: probes port 502 across the given range/list.
Response: `{ "found": [{ "unit_id": int, "host": str|null, "responded_ms": int }] }`.

### `POST /api/test/write`
Tier: operator, and only accepted while the terminal is in **commissioning mode** (a separate toggle, itself admin-gated, that must be explicitly enabled — see Phase 6 safety gating).
Body: `{ "point": str, "value": bool, "confirm": true }`.
Auto-reverts after a fixed timeout (default 5s) regardless of client behavior, so a dropped connection can never leave a test output energized.
`409` if commissioning mode is off.

### `POST /api/factory-reset`
Tier: admin. Body: `{ "confirm_token": str }` (token shown once in the UI, must be re-typed — not just a checkbox, given this generates a new `boot_id`).
Wipes `/etc/io_config.json` and `/etc/system_config.json` to defaults, generates a **new** `boot_id` in `/etc/ctrl_id.json` (reference §5 — this is the *only* code path allowed to do that).

### OTA (Phase 7, stubbed here for contract completeness)
- `GET /api/ota/status` — tier admin, current app/schema version + last update result.
- `POST /api/ota/apply` — tier admin, triggers staged download → verify → backup → migrate → swap → health-check → auto-rollback-on-failure sequence described in the plan.

## Error shape (all endpoints)

```json
{ "error": "validation_failed" | "unauthorized" | "forbidden" | "conflict" | "not_found",
  "problems": ["optional array of human-readable detail strings"] }
```

HTTP status always matches: `401` unauthorized, `403` forbidden (wrong tier), `404` not_found, `409` conflict, `422` validation_failed.
