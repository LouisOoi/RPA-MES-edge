"""
Config-driven poll engine — the Phase 2 replacement for modbus_poll.py.

What changed from the hardcoded script:
  - No IO_MODULE_ADDR / PLC_ADDR / BUTTON_COIL / LED_COIL / FAULT_REGISTER
    constants. The poll plan and every address come from a validated
    io_config document (build_plan / point_io.py).
  - Client selection (RTU serial vs per-device TCP) comes from
    `bus.transport`, not a hardcoded ModbusSerialClient call.
  - Results land in a shared LiveSnapshot instead of module-level globals,
    so a separate process (the Flask config UI) can read current values
    without ever touching the Modbus bus itself.
  - A TCP read that times out is marked stale in the snapshot, never
    coerced to a fake reading.

What deliberately did NOT move here: the button->LED and fault-threshold
*business* logic (what to do about a value) — that's Phase 3's rule engine,
wired in optionally below via `rule_engine=`. What DID move here in Phase 3:
debounce (see debounce.py) — it's signal conditioning on the raw read
itself, not a decision about what the value means, so it belongs in the
poll path regardless of whether any rule ever looks at the point.

Phase 4 adds hot reload (`reload()` + an optional `config_path=` watcher in
`run_cycle()`): a config change is validated BEFORE anything about the
running engine is touched, every current digital_out is driven to its
`safe_state` before the swap, and the old in-memory config is preserved as
`.lkg` for `rollback_to_lkg()`. An invalid config never gets past
`validate_io()`, so the running plan/clients/rule_engine are provably
untouched on rejection — nothing is reassigned until validation has already
succeeded.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from validators import ConfigValidationError, validate_io

from . import config_store
from .debounce import Debouncer
from .event_store import log_event
from .live_snapshot import LiveSnapshot
from .modbus_clients import build_clients, close_all, connect_all
from .point_io import ReadResult, read_point
from .point_io import write_point as _write_point_io
from .poll_plan import build_plan
from .test_write import TestWriteManager, TestWriteResult


@dataclass(frozen=True)
class ReloadResult:
    ok: bool
    problems: list[str] = field(default_factory=list)


class PollEngine:
    def __init__(
        self,
        io_config: dict,
        ident: dict,
        db_path: str | Path,
        *,
        clients: dict[int, Any] | None = None,
        rule_engine: Any | None = None,
        config_path: str | Path | None = None,
        clients_factory: Any | None = None,
    ) -> None:
        validate_io(io_config)  # never run against an unvalidated config
        self.io_config = io_config
        self.ident = ident
        self.db_path = db_path
        # Used by reload() whenever it isn't handed an explicit `clients=`
        # override, so tests can inject a fake-client factory once at
        # construction instead of passing clients= on every reload() call
        # (production never sets this — build_clients talks to real pymodbus).
        self._clients_factory = clients_factory if clients_factory is not None else build_clients
        self.clients = clients if clients is not None else self._clients_factory(io_config)
        self.plan = build_plan(io_config)
        self.snapshot = LiveSnapshot()
        self.rule_engine = rule_engine  # Phase 3, optional — see module docstring
        self.test_write_manager = TestWriteManager()  # Phase 6, always present — see test_write.py
        self._debouncer = Debouncer(io_config["bus"]["poll_interval_ms"])

        self._device_by_unit_id = {d["unit_id"]: d for d in io_config["devices"]}
        self._point_by_id = {p["id"]: p for p in io_config["points"]}
        self._stale_state: dict[str, bool] = {}
        self._owns_clients = clients is None

        self._config_path = Path(config_path) if config_path is not None else None
        self._watcher = (
            config_store.ConfigWatcher(self._config_path, initial_version=io_config.get("config_version"))
            if self._config_path is not None
            else None
        )

    def connect(self) -> None:
        connect_all(self.clients)

    def close(self) -> None:
        if self._owns_clients:
            close_all(self.clients)

    def run_cycle(self, *, now_ms: int | None = None) -> dict[str, ReadResult]:
        """One poll pass over every readable point: read -> debounce ->
        snapshot -> (optionally) rule evaluation. Returns {point_id:
        ReadResult} of the EFFECTIVE (debounced) values for the cycle,
        mainly so tests/exit-criteria can assert on exact values without
        going back through the snapshot.

        `now_ms` is accepted so tests can drive deterministic time (e.g.
        for pulse-revert timing) instead of depending on wall clock."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)

        if self._watcher is not None:
            candidate = self._watcher.poll()
            if candidate is not None:
                result = self.reload(candidate)
                event_type = "config_reload_applied" if result.ok else "config_reload_rejected"
                log_event(self.db_path, self.ident, event_type, {
                    "config_version": candidate.get("config_version"),
                    "problems": result.problems,
                })
                # Whether applied or rejected, THIS cycle still polls below
                # with whichever plan is now current — a config change (or
                # a bad one) never costs a missed poll cycle.

        self.test_write_manager.apply_pending_reverts(self, now_ms)

        results: dict[str, ReadResult] = {}
        for entry in self.plan:
            device = entry.device
            client = self.clients[device["unit_id"]]
            for point in entry.points:
                if point["modbus"]["fn"] == "write_coil":
                    continue  # outputs are not polled on the read path
                raw_result = read_point(client, device, point)
                effective = self._debounce(point, raw_result)
                self.snapshot.update(point["id"], effective.value, effective.stale)
                self._log_stale_transition(point, effective)
                results[point["id"]] = effective

        if self.rule_engine is not None:
            self.rule_engine.evaluate_cycle(self, results, now_ms=now_ms)

        return results

    def _debounce(self, point: dict, raw_result: ReadResult) -> ReadResult:
        if raw_result.stale or point["kind"] != "digital_in" or not point.get("debounce_ms"):
            return raw_result
        effective_value = self._debouncer.apply(point["id"], point["debounce_ms"], raw_result.value)
        return ReadResult(value=effective_value, stale=False)

    def reload(self, new_io_config: dict, *, clients: dict[int, Any] | None = None) -> ReloadResult:
        """Validate-then-swap hot reload. On any rejection, `self.*` is
        GUARANTEED untouched — every reassignment below happens only after
        validation and every fallible build step has already succeeded, so
        there is no window where a bad config could have partially applied.
        """
        try:
            validate_io(new_io_config)
        except ConfigValidationError as exc:
            return ReloadResult(ok=False, problems=exc.problems)

        # Drive every CURRENT digital_out to its safe_state using the OLD
        # mapping, before anything is swapped. This is the point of doing
        # it here rather than after: once the swap happens, if the new
        # config re-addresses or drops this point, nothing running any
        # longer has a handle on the physical output to turn it off.
        for point in self.io_config["points"]:
            if point["kind"] == "digital_out":
                self.write_point(point["id"], point.get("safe_state", False))

        try:
            new_clients = clients if clients is not None else self._clients_factory(new_io_config)
            new_plan = build_plan(new_io_config)
        except Exception as exc:  # noqa: BLE001 - build failure must not corrupt running state
            return ReloadResult(ok=False, problems=[f"failed to build new poll plan: {exc}"])

        new_rule_engine = None
        if self.rule_engine is not None:
            from .rule_engine import RuleEngine  # local import: avoids a module-level cycle risk
            new_rule_engine = RuleEngine(new_io_config.get("rules", []), self.ident, self.db_path)

        if self._config_path is not None:
            config_store.backup_as_lkg(self._config_path, self.io_config)
            if self.io_config.get("config_version") is not None:
                config_store.save_version(self._config_path, self.io_config)

        old_clients = self.clients
        old_owns_clients = self._owns_clients

        self.io_config = new_io_config
        self.clients = new_clients
        self.plan = new_plan
        self.rule_engine = new_rule_engine
        self._debouncer = Debouncer(new_io_config["bus"]["poll_interval_ms"])
        self._device_by_unit_id = {d["unit_id"]: d for d in new_io_config["devices"]}
        self._point_by_id = {p["id"]: p for p in new_io_config["points"]}
        self._owns_clients = clients is None

        if old_owns_clients:
            close_all(old_clients)

        if self._watcher is not None:
            self._watcher.mark_seen(new_io_config.get("config_version"))

        return ReloadResult(ok=True)

    def rollback_to_lkg(self) -> ReloadResult:
        if self._config_path is None:
            return ReloadResult(ok=False, problems=["no config_path configured; nothing to roll back to"])
        try:
            lkg_doc = config_store.read_json(config_store.lkg_path(self._config_path))
        except (FileNotFoundError, ValueError) as exc:
            return ReloadResult(ok=False, problems=[f"no usable LKG config: {exc}"])
        return self.reload(lkg_doc)

    def rollback_to_version(self, version: int) -> ReloadResult:
        """Like rollback_to_lkg but can go back further than one step —
        Phase 6's "version history + one-click rollback," reusing the same
        validated reload() path rather than a special-cased trust path."""
        if self._config_path is None:
            return ReloadResult(ok=False, problems=["no config_path configured; nothing to roll back to"])
        try:
            doc = config_store.read_version(self._config_path, version)
        except FileNotFoundError:
            return ReloadResult(ok=False, problems=[f"no saved version {version}"])
        return self.reload(doc)

    def list_config_versions(self) -> list[int]:
        if self._config_path is None:
            return []
        return config_store.list_versions(self._config_path)

    def request_test_write(
        self, point_id: str, value: bool, *, confirm: bool, timeout_ms: int = 5000, now_ms: int | None = None,
    ) -> TestWriteResult:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return self.test_write_manager.request_write(
            self, point_id, value, confirm=confirm, timeout_ms=timeout_ms, now_ms=now_ms,
        )

    def write_point(self, point_id: str, value: bool) -> ReadResult:
        point = self._point_by_id[point_id]
        device = self._device_by_unit_id[point["unit_id"]]
        client = self.clients[device["unit_id"]]

        result = _write_point_io(client, device, point, value)
        if result.stale:
            log_event(self.db_path, self.ident, "bus_write_error", {
                "point": point_id, "unit_id": point["unit_id"], "error": result.error,
            })
        else:
            self.snapshot.update(point_id, value, False)
        return result

    def _log_stale_transition(self, point: dict, result: ReadResult) -> None:
        point_id = point["id"]
        was_stale = self._stale_state.get(point_id, False)
        if result.stale and not was_stale:
            log_event(self.db_path, self.ident, "bus_read_error", {
                "point": point_id, "unit_id": point["unit_id"], "error": result.error,
            })
        elif not result.stale and was_stale:
            log_event(self.db_path, self.ident, "bus_read_recovered", {
                "point": point_id, "unit_id": point["unit_id"],
            })
        self._stale_state[point_id] = result.stale
