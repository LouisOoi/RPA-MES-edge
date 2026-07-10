"""
Bus Scan — Phase 6. Transport-aware per the plan: "RTU scans unit ids 1-32
on the serial line; TCP pings a user-supplied IP range (or list) on port
502 and reports which modules answer."

RTU scan uses a real Modbus read (read_coils at address 0, count 1) rather
than any lower-level probe, because the only thing that actually tells you
"a slave answered" on an RS485 bus is a valid Modbus response — there's no
separate link-layer ping. TCP scan uses a raw socket connect (same
reachability-only scope as system_store.check_mqtt_connection, and for the
same reason: proving a full Modbus TCP handshake per candidate IP across a
whole /24 would be slow and isn't what "which modules answer" needs).
"""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanHit:
    unit_id: int | None
    host: str | None
    responded_ms: float


def parse_ip_range(spec: str) -> list[str]:
    """Accepts "192.168.10.10-192.168.10.20" (inclusive range) or a
    comma-separated list "192.168.10.10,192.168.10.12". Raises ValueError
    on anything else rather than guessing."""
    spec = spec.strip()
    if "," in spec:
        return [ip.strip() for ip in spec.split(",") if ip.strip()]
    if "-" in spec:
        start_s, end_s = (p.strip() for p in spec.split("-", 1))
        start, end = ipaddress.IPv4Address(start_s), ipaddress.IPv4Address(end_s)
        if end < start:
            raise ValueError(f"range end {end} is before start {start}")
        return [str(ipaddress.IPv4Address(int(start) + i)) for i in range(int(end) - int(start) + 1)]
    return [spec]  # single address


def scan_rtu(client, *, unit_id_range: range = range(1, 33), timeout_s: float = 0.5) -> list[ScanHit]:
    hits: list[ScanHit] = []
    for unit_id in unit_id_range:
        start = time.monotonic()
        try:
            resp = client.read_coils(0, count=1, device_id=unit_id)
            responded = not resp.isError()
        except Exception:  # noqa: BLE001 - any transport failure just means "no answer"
            responded = False
        elapsed_ms = (time.monotonic() - start) * 1000
        if responded:
            hits.append(ScanHit(unit_id=unit_id, host=None, responded_ms=elapsed_ms))
    return hits


def scan_tcp(ip_range: str, *, port: int = 502, timeout_s: float = 0.3) -> list[ScanHit]:
    hits: list[ScanHit] = []
    for host in parse_ip_range(ip_range):
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                elapsed_ms = (time.monotonic() - start) * 1000
                hits.append(ScanHit(unit_id=None, host=host, responded_ms=elapsed_ms))
        except OSError:
            continue
    return hits
