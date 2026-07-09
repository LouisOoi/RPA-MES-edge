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
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from validators import validate_io

from .debounce import Debouncer
from .event_store import log_event
from .live_snapshot import LiveSnapshot
from .modbus_clients import build_clients, close_all, connect_all
from .point_io import ReadResult, read_point
from .point_io import write_point as _write_point_io
from .poll_plan import build_plan


class PollEngine:
    def __init__(
        self,
        io_config: dict,
        ident: dict,
        db_path: str | Path,
        *,
        clients: dict[int, Any] | None = None,
        rule_engine: Any | None = None,
    ) -> None:
        validate_io(io_config)  # never run against an unvalidated config
        self.io_config = io_config
        self.ident = ident
        self.db_path = db_path
        self.clients = clients if clients is not None else build_clients(io_config)
        self.plan = build_plan(io_config)
        self.snapshot = LiveSnapshot()
        self.rule_engine = rule_engine  # Phase 3, optional — see module docstring
        self._debouncer = Debouncer(io_config["bus"]["poll_interval_ms"])

        self._device_by_unit_id = {d["unit_id"]: d for d in io_config["devices"]}
        self._point_by_id = {p["id"]: p for p in io_config["points"]}
        self._stale_state: dict[str, bool] = {}
        self._owns_clients = clients is None

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
