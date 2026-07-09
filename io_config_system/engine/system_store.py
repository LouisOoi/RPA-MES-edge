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
from dataclasses import dataclass
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
    return result


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    message: str = ""


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


def check_mqtt_connection(host: str, port: int, *, timeout_s: float = 3.0) -> ApplyResult:
    """TCP-level reachability only — see module docstring for why this
    isn't a full MQTT handshake."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return ApplyResult(ok=True, message=f"TCP connect to {host}:{port} succeeded")
    except OSError as exc:
        return ApplyResult(ok=False, message=f"TCP connect to {host}:{port} failed: {exc}")
