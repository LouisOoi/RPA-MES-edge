"""
Generic point read/write dispatch. One function per direction, driven
entirely by `point['modbus']['fn']` — no per-site branching in the engine
itself, which is the actual behavior change from modbus_poll.py (it called
`client.read_coils(BUTTON_COIL, 1, slave=IO_MODULE_ADDR)` literally; this
calls whatever `fn` the config says, against whatever address the config
says).

A read that errors or times out returns `stale=True` and `value=None` —
it is never coerced to 0/False. The plan is explicit about this for TCP
("a timed-out read as 'stale', not 'zero'") and the same logic applies to a
non-responding RTU slave: a dropped bus response is not the same fact as a
sensor reading zero, and treating them the same is how a Wi-Fi hiccup used
to be able to fake a value or mis-fire downstream logic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReadResult:
    value: Any  # bool for digital_in, int for register/analog_in; None if stale
    stale: bool
    error: str | None = None


def _device_id_for(device: dict) -> int:
    """The Modbus wire unit-identifier byte. For RTU this IS the device's
    unit_id (the RS485 slave address). For TCP it's the optional, usually
    irrelevant device['tcp']['slave_id'] (default 1) — see the NOTE in
    validators.py about unit_id being purely this config's device key on
    the TCP path."""
    if "tcp" in device:
        return device["tcp"].get("slave_id", 1)
    return device["unit_id"]


def read_point(client, device: dict, point: dict, *, comms: dict | None = None) -> ReadResult:
    """AR-08: `comms` (see engine/device_health.py's resolve_comms()) adds
    per-slave retry-with-backoff on top of the single-attempt behavior
    this had before. Omitting `comms` (every call site before AR-08, and
    every direct test of this function) is exactly retries=0, backoff=0
    — i.e. identical to the old one-shot behavior; this is additive, not
    a change to existing callers.

    write_coil points are outputs; there is nothing to poll-read. The
    engine skips these on the read path (see poll_engine.py) — this is a
    genuine caller error, so it still raises immediately rather than
    getting the retry treatment."""
    comms = comms or {}
    retries = comms.get("retries", 0)
    backoff_ms = comms.get("backoff_ms", 0)
    modbus = point["modbus"]
    fn = modbus["fn"]
    device_id = _device_id_for(device)

    if fn == "write_coil":
        raise ValueError(f"point '{point['id']}' has fn=write_coil, which is not readable")
    if fn not in ("read_coils", "read_input_registers", "read_holding_registers"):
        raise ValueError(f"unknown modbus fn: {fn!r}")

    last_error: str | None = None
    for attempt in range(retries + 1):
        try:
            if fn == "read_coils":
                resp = client.read_coils(modbus["address"], count=modbus.get("count", 1), device_id=device_id)
                values = None if resp.isError() else resp.bits
            elif fn == "read_input_registers":
                resp = client.read_input_registers(modbus["address"], count=modbus.get("count", 1), device_id=device_id)
                values = None if resp.isError() else resp.registers
            else:  # read_holding_registers
                resp = client.read_holding_registers(modbus["address"], count=modbus.get("count", 1), device_id=device_id)
                values = None if resp.isError() else resp.registers

            if values is None:
                last_error = str(resp)
            else:
                value = values[0] if modbus.get("count", 1) == 1 else list(values[: modbus["count"]])
                if point.get("invert") and isinstance(value, bool):
                    value = not value
                return ReadResult(value=value, stale=False)
        except Exception as exc:  # noqa: BLE001 - any transport failure => stale, never a crash
            last_error = str(exc)

        if attempt < retries and backoff_ms:
            time.sleep(backoff_ms / 1000)

    return ReadResult(value=None, stale=True, error=last_error)


def write_point(client, device: dict, point: dict, value: bool) -> ReadResult:
    modbus = point["modbus"]
    if modbus["fn"] != "write_coil":
        raise ValueError(f"point '{point['id']}' is not a write_coil point (fn={modbus['fn']!r})")

    device_id = _device_id_for(device)
    out_value = (not value) if point.get("invert") else value

    try:
        resp = client.write_coil(modbus["address"], out_value, device_id=device_id)
        if resp.isError():
            return ReadResult(value=None, stale=True, error=str(resp))
    except Exception as exc:  # noqa: BLE001
        return ReadResult(value=None, stale=True, error=str(exc))

    return ReadResult(value=value, stale=False)
