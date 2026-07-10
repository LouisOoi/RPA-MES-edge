"""
Admin-tier network/MQTT-broker/time config — Phase 5.

Honest limits of what this module can be, running in a dev sandbox with no
real Pi under it: the plan says "a privileged helper applies OS-level
network/time changes; the app only writes validated intent." This module
implements the "writes validated intent" half completely. The privileged
helper half is a `NetworkApplier` interface with exactly one real
implementation possible right now — `NullNetworkApplier`, which validates
and records what it WOULD do without touching the OS. There is no way to
verify a real `dhcpcd.conf`/`netplan`/`timedatectl` integration without an
actual Pi to run it on, and pretending to implement one here that's never
been run against real hardware would be worse than being explicit that it
doesn't exist yet. Swapping in a real applier on-device is a small, isolated
change BECAUSE this interface exists — that's the point of it.

Similarly, the MQTT "Test connection" button is implemented as a raw TCP
reachability check (can we open a socket to host:port within the timeout),
not a full MQTT CONNECT handshake — that would need the `paho-mqtt`
dependency and a real broker to test against, neither of which is in scope
to add speculatively. A TCP-level check already answers the two most common
"why won't this unit connect" questions (wrong host/port, firewall/VLAN
blocking it) even though it can't confirm TLS handshake or MQTT auth.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from validators import ConfigValidationError, validate_system

from . import config_store


class SystemUpdateError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def load_system_config(path: str | Path) -> dict:
    return config_store.read_json(path)


def save_system_config(path: str | Path, new_config: dict, applier: "NetworkApplier") -> "ApplyResult":
    """Validates, applies (via the injected applier), and only then
    persists. If the applier reports failure, nothing is written to disk —
    a network change that the privileged helper couldn't actually carry out
    must not be recorded as if it had been."""
    try:
        validate_system(new_config)
    except ConfigValidationError as exc:
        raise SystemUpdateError(exc.problems) from exc

    result = applier.apply(new_config)
    if not result.ok:
        return result

    config_store.atomic_write_json(path, new_config)
    warnings = [
        f"'{h}' looks like a public NTP pool — on an isolated OT VLAN with no "
        f"internet route, this will silently never sync (AR-04). Confirm this "
        f"site's VLAN genuinely has an internet route, or point at a local NTP "
        f"source instead."
        for h in flag_public_ntp_hosts(new_config.get("time", {}).get("ntp", []))
    ] + [
        f"'{k}' is an MQTT client private key stored as a plain, extractable file "
        f"(AR-06). Migrate to hardware-backed key storage (engine/key_storage.py) "
        f"before shipping to a customer that runs a security review."
        for k in flag_extractable_client_key(new_config.get("mqtt", {}))
    ]
    if warnings:
        result = ApplyResult(ok=result.ok, message=result.message, warnings=warnings)
    return result


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    message: str = ""
    warnings: list[str] = field(default_factory=list)


class NetworkApplier(Protocol):
    def apply(self, system_config: dict) -> ApplyResult: ...


class NullNetworkApplier:
    """Validates the shape of the request and reports success without
    touching the OS. This is what runs in dev/tests and, honestly, is the
    only implementation that exists right now — see module docstring."""

    def __init__(self) -> None:
        self.applied: list[dict] = []  # test/inspection hook

    def apply(self, system_config: dict) -> ApplyResult:
        self.applied.append(system_config)
        return ApplyResult(ok=True, message="recorded, no OS changes made (NullNetworkApplier)")


class AlwaysFailNetworkApplier:
    """Test double for the "privileged helper couldn't apply it" path —
    e.g. a real implementation would return this shape if `netplan apply`
    or `timedatectl` exited non-zero."""

    def __init__(self, message: str = "simulated apply failure") -> None:
        self._message = message

    def apply(self, system_config: dict) -> ApplyResult:
        return ApplyResult(ok=False, message=self._message)


_PUBLIC_NTP_HINTS = ("pool.ntp.org", "time.google.com", "time.windows.com", "time.apple.com", "time.cloudflare.com")


def flag_public_ntp_hosts(ntp_hosts: list[str]) -> list[str]:
    """AR-04: the security posture mandates an isolated OT VLAN with no
    internet route, but the seed/plan default used to be `pool.ntp.org`,
    which needs one — on an isolated segment the clock would silently
    never sync. This is a WARNING, not a validation rejection: a site that
    genuinely does have (or chooses to give this VLAN) an internet route
    can legitimately still want a public pool. Callers (the API layer)
    surface these as non-blocking warnings in the save response so a
    commissioning engineer notices and confirms it's intentional, instead
    of finding out three weeks later that the clock never synced."""
    return [h for h in ntp_hosts if any(hint in h for hint in _PUBLIC_NTP_HINTS)]


def flag_extractable_client_key(mqtt_config: dict) -> list[str]:
    """AR-06: `system.mqtt.client_key` is, in every deployment of this
    codebase today, a plain file path — the only KeyStore implementation
    that exists is `engine.key_storage.FileKeyStore`, which is honestly
    extractable by design (see that module's docstring for why a real
    hardware-backed implementation isn't faked here). This returns the
    key path(s) that should be flagged so the API layer can surface a
    non-blocking warning, same posture as `flag_public_ntp_hosts` above:
    a site may have a real reason this is still acceptable for now, but a
    commissioning engineer should see the gap on every save rather than
    finding out during a security review."""
    client_key = mqtt_config.get("client_key")
    return [client_key] if client_key else []


def check_mqtt_connection(host: str, port: int, *, timeout_s: float = 3.0) -> ApplyResult:
    """TCP-level reachability only — see module docstring for why this
    isn't a full MQTT handshake."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return ApplyResult(ok=True, message=f"TCP connect to {host}:{port} succeeded")
    except OSError as exc:
        return ApplyResult(ok=False, message=f"TCP connect to {host}:{port} failed: {exc}")
