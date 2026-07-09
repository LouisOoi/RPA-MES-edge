"""
Config-driven Modbus client factory.

Replaces the hardcoded `ModbusSerialClient(port=..., baudrate=...)` line at
the top of the reference's modbus_poll.py. The client(s) to build are chosen
from `bus.transport`, not from constants — that's the whole point of Phase 2.

Pinned to pymodbus 3.13's API: read/write calls take `device_id=`, not the
older `slave=` kwarg. If this is ever run against an older pymodbus, the
kwarg name is the thing that will break first.
"""
from __future__ import annotations

from typing import Any

from pymodbus.client import ModbusSerialClient, ModbusTcpClient


def build_rtu_client(bus: dict) -> ModbusSerialClient:
    s = bus["serial"]
    return ModbusSerialClient(
        port=s["port"],
        baudrate=s["baudrate"],
        parity=s["parity"],
        stopbits=s["stopbits"],
        bytesize=s["bytesize"],
    )


def build_tcp_client(bus: dict, device: dict) -> ModbusTcpClient:
    tcp_defaults = bus.get("tcp", {})
    return ModbusTcpClient(
        host=device["tcp"]["host"],
        port=tcp_defaults.get("port", 502),
        timeout=tcp_defaults.get("timeout_ms", 800) / 1000,
        retries=tcp_defaults.get("retries", 2),
    )


def build_clients(io_config: dict) -> dict[int, Any]:
    """Returns {device['unit_id']: connected-client-instance}.

    RTU: every device maps to the SAME shared serial client object; the
    per-device distinction happens at call time via the device_id argument
    (RS485 slave address). TCP: each device gets its own client bound to
    its own host, since each module is its own TCP server (plan's resolved
    TCP topology). validators.validate_io already guarantees unit_id is
    unique per device before this is ever called.
    """
    bus = io_config["bus"]
    clients: dict[int, Any] = {}
    if bus["transport"] == "rtu":
        shared = build_rtu_client(bus)
        for d in io_config["devices"]:
            clients[d["unit_id"]] = shared
    elif bus["transport"] == "tcp":
        for d in io_config["devices"]:
            clients[d["unit_id"]] = build_tcp_client(bus, d)
    else:
        raise ValueError(f"unknown transport: {bus['transport']!r}")
    return clients


def connect_all(clients: dict[int, Any]) -> None:
    seen = set()
    for client in clients.values():
        if id(client) not in seen:
            client.connect()
            seen.add(id(client))


def close_all(clients: dict[int, Any]) -> None:
    seen = set()
    for client in clients.values():
        if id(client) not in seen:
            client.close()
            seen.add(id(client))
