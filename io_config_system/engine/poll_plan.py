"""
Builds the poll plan from `points[]` — this is the direct replacement for
modbus_poll.py's hardcoded IO_MODULE_ADDR / PLC_ADDR / BUTTON_COIL / LED_COIL
/ FAULT_REGISTER constants. Everything the old script knew at write-time,
this module derives at load-time from a validated io_config document.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanEntry:
    device: dict
    points: tuple[dict, ...]


def build_plan(io_config: dict) -> list[PlanEntry]:
    """Group points by their device (unit_id), in device declaration order,
    then point declaration order within each device. Grouping per device
    (rather than firing one Modbus transaction per point) is what the
    execution plan calls "grouped reads per device" — a real optimization
    (coalescing adjacent addresses into one read) is deferred; this phase
    establishes the config-driven structure it will slot into later.
    """
    device_by_unit_id = {d["unit_id"]: d for d in io_config["devices"]}
    points_by_unit_id: dict[int, list[dict]] = {d["unit_id"]: [] for d in io_config["devices"]}
    for p in io_config["points"]:
        points_by_unit_id[p["unit_id"]].append(p)

    return [
        PlanEntry(device=device_by_unit_id[unit_id], points=tuple(points))
        for unit_id, points in points_by_unit_id.items()
        if points
    ]
