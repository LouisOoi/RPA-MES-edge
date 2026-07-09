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

What deliberately did NOT move here: the button-debounce-then-pulse-LED and
fault-register-threshold business logic. In the old script that logic was
hardcoded Python; in the new model it is supposed to become data (rules[]),
which is Phase 3's charter ("Evaluate rules[] each cycle"). This engine's
job is only to keep the live snapshot correct and expose a generic
`write_point()` that Phase 3's rule actions (and Phase 6's Test Write) will
call — it does not decide *when* to write anything on its own.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from validators import validate_io

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
    ) -> None:
        validate_io(io_config)  # never run against an unvalidated config
        self.io_config = io_config
        self.ident = ident
        self.db_path = db_path
        self.clients = clients if clients is not None else build_clients(io_config)
        self.plan = build_plan(io_config)
        self.snapshot = LiveSnapshot()

        self._device_by_unit_id = {d["unit_id"]: d for d in io_config["devices"]}
        self._point_by_id = {p["id"]: p for p in io_config["points"]}
        self._stale_state: dict[str, bool] = {}
        self._owns_clients = clients is None

    def connect(self) -> None:
        connect_all(self.clients)

    def close(self) -> None:
        if self._owns_clients:
            close_all(self.clients)

    def run_cycle(self) -> dict[str, ReadResult]:
        """One poll pass over every readable point. Returns {point_id:
        ReadResult} for the cycle, mainly so tests/exit-criteria can assert
        on exact values without going back through the snapshot."""
        results: dict[str, ReadResult] = {}
        for entry in self.plan:
            device = entry.device
            client = self.clients[device["unit_id"]]
            for point in entry.points:
                if point["modbus"]["fn"] == "write_coil":
                    continue  # outputs are not polled on the read path
                result = read_point(client, device, point)
                self.snapshot.update(point["id"], result.value, result.stale)
                self._log_stale_transition(point, result)
                results[point["id"]] = result
        return results

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
