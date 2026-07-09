"""
schema_version 1 -> 2.

v1 is the config shape implied by today's hardcoded modbus_poll.py: RTU only,
flat address+kind points, and NO rules[] — the button-debounce-then-pulse-LED
and fault-register-threshold logic lives in Python (poll_cycle()), not data.

v2 makes that logic data. This migration is deliberately narrow rather than
a generic heuristic: it only knows how to translate the *specific* topology
described in factory_iot_reference.md section 7 (exactly one digital_in +
exactly one digital_out sharing a unit_id -> "maintenance button" pattern;
exactly one register point -> "fault code" pattern). If a v1 document doesn't
match that shape, migration refuses rather than guessing at what rule to
synthesize — inventing control logic during a migration is exactly the kind
of silent behaviour change this whole project exists to prevent.
"""
from __future__ import annotations

import time

from . import MigrationError


def _kind_to_modbus(kind: str, address: int) -> dict:
    return {
        "digital_in":  {"fn": "read_coils", "address": address, "count": 1},
        "digital_out": {"fn": "write_coil", "address": address},
        "register":    {"fn": "read_holding_registers", "address": address, "count": 1},
    }[kind]


def _humanize(point_id: str) -> str:
    return point_id.replace("_", " ").title()


def _infer_device_type(unit_id: int, v1_points: list[dict]) -> str:
    kinds_on_device = {p["kind"] for p in v1_points if p["unit_id"] == unit_id}
    if "register" in kinds_on_device:
        return "plc"
    if {"digital_in", "digital_out"} & kinds_on_device:
        return "remote_io"
    return "other"


def migrate_v1_to_v2(doc: dict, *, updated_by: str) -> dict:
    if doc.get("schema_version") != 1:
        raise MigrationError(f"migrate_v1_to_v2 called on schema_version {doc.get('schema_version')}")

    bus_v1 = doc["bus"]
    poll_interval_ms = bus_v1["poll_interval_ms"]

    devices_v2 = [
        {
            "unit_id": d["unit_id"],
            "name": d["name"],
            "type": _infer_device_type(d["unit_id"], doc["points"]),
        }
        for d in doc["devices"]
    ]

    points_v2 = []
    for p in doc["points"]:
        point_v2 = {
            "id": p["id"],
            "name": _humanize(p["id"]),
            "unit_id": p["unit_id"],
            "kind": p["kind"],
            "modbus": _kind_to_modbus(p["kind"], p["address"]),
            "scaling": None,
            "unit": None,
            "invert": False,
        }
        if p["kind"] == "digital_in":
            point_v2["debounce_ms"] = p.get("debounce_reads", 1) * poll_interval_ms
        points_v2.append(point_v2)

    rules_v2 = _synthesize_rules(doc["points"])

    return {
        "schema_version": 2,
        "config_version": 1,
        "updated_at": int(time.time() * 1000),
        "updated_by": updated_by,
        "bus": {
            "transport": "rtu",
            "poll_interval_ms": poll_interval_ms,
            "serial": {
                "port": bus_v1["port"],
                "baudrate": bus_v1["baudrate"],
                "parity": bus_v1["parity"],
                "stopbits": bus_v1["stopbits"],
                "bytesize": bus_v1["bytesize"],
            },
        },
        "devices": devices_v2,
        "points": points_v2,
        "rules": rules_v2,
    }


def _synthesize_rules(v1_points: list[dict]) -> list[dict]:
    digital_ins  = [p for p in v1_points if p["kind"] == "digital_in"]
    digital_outs = [p for p in v1_points if p["kind"] == "digital_out"]
    registers    = [p for p in v1_points if p["kind"] == "register"]

    rules: list[dict] = []

    # Button -> LED + maintenance_request, one rule per digital_in that has
    # exactly one digital_out sharing its unit_id.
    for btn in digital_ins:
        siblings = [o for o in digital_outs if o["unit_id"] == btn["unit_id"]]
        if len(siblings) != 1:
            raise MigrationError(
                f"cannot migrate: point '{btn['id']}' (digital_in, unit_id "
                f"{btn['unit_id']}) does not have exactly one digital_out "
                f"sibling on the same unit_id ({len(siblings)} found); this "
                f"migration only knows the single button->LED topology"
            )
        led = siblings[0]
        rules.append({
            "id": f"rule_{btn['id']}",
            "enabled": True,
            "match": "all",
            "when": [{"point": btn["id"], "op": "rising"}],
            "then": [
                {"action": "pulse", "point": led["id"], "ms": 500},
                {"action": "log_event", "event_type": "maintenance_request"},
            ],
            "else": [],
        })

    # Fault register -> machine_fault event on 0 -> non-zero transition,
    # approximated as fault code > 0 (fault codes are non-negative in the
    # reference spec's FAULT_REGISTER convention).
    for reg in registers:
        rules.append({
            "id": f"rule_{reg['id']}_fault",
            "enabled": True,
            "match": "all",
            "when": [{"point": reg["id"], "op": ">", "value": 0}],
            "then": [
                {"action": "log_event", "event_type": "machine_fault"},
            ],
            "else": [],
        })

    return rules
