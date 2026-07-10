"""
AR-09 — central config backup + drift reporting (minimal). See
IO_Config_Execution_Plan.md's amended "Where the UI runs" row: every
successful config apply pushes a non-secret copy of the io_config to a
central backup store over the existing MQTT/TLS channel, one-way and
best-effort — for fleet drift reporting and RMA restore, never as a
control dependency. The device must keep working exactly as before if
the server is unreachable, or doesn't exist at all.

Honest scope, same posture as engine/watchdog.py and engine/key_storage.py:
this repo is the DEVICE side. The plan itself calls the receiving half "a
new central-side component" that isn't part of io_config_system/ at all —
there is nothing here to push TO, and no MQTT client dependency exists yet
in this codebase to build a real publisher against. What this module
provides: the `ConfigBackupClient` interface a real MQTT publish would
satisfy, the honest no-op default (`NullConfigBackupClient` — records
intent, sends nothing), and `compute_config_fingerprint()`, the one piece
of real, useful logic that doesn't need a server to exist: a stable
(config_version, content hash) pair that any future central component
would need in order to ever answer "has this device drifted from what we
last saw?" at all. io_config itself carries no secrets (credentials live
only in system_config, never touched here), so "non-secret copy" is
already what this pushes without any redaction step.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ConfigFingerprint:
    config_version: int | None
    content_hash: str
    computed_at_ms: int


def compute_config_fingerprint(io_config: dict, *, now_ms: int | None = None) -> ConfigFingerprint:
    """A stable fingerprint of an io_config document: its declared
    config_version plus a content hash of the document as it actually
    is. Two devices (or one device across time) with the same
    config_version but a different content_hash have drifted from each
    other even though the version number alone would look identical —
    the whole reason a hash is included and not just the version."""
    canonical = json.dumps(io_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    content_hash = hashlib.sha256(canonical).hexdigest()
    resolved_now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return ConfigFingerprint(
        config_version=io_config.get("config_version"),
        content_hash=content_hash,
        computed_at_ms=resolved_now_ms,
    )


@dataclass(frozen=True)
class BackupPushResult:
    ok: bool
    message: str = ""


class ConfigBackupClient(Protocol):
    def push(self, ident: dict, io_config: dict, fingerprint: ConfigFingerprint) -> BackupPushResult: ...


class NullConfigBackupClient:
    """The only implementation that exists today — see module docstring
    for why a real MQTT-backed publisher isn't faked here without a
    broker/central component to test it against. Records what it would
    have pushed (test/inspection hook: `.pushed`) and reports success,
    honestly meaning 'recorded locally,' never 'a central server now has
    a copy' — there is no central server behind this default."""

    def __init__(self) -> None:
        self.pushed: list[tuple[dict, dict, ConfigFingerprint]] = []

    def push(self, ident: dict, io_config: dict, fingerprint: ConfigFingerprint) -> BackupPushResult:
        self.pushed.append((dict(ident), dict(io_config), fingerprint))
        return BackupPushResult(
            ok=True, message="recorded locally, no central server configured (NullConfigBackupClient)",
        )


class AlwaysFailBackupClient:
    """Test double for "the central server was unreachable." Exists to
    prove the best-effort contract: a failed backup push must never fail,
    delay, or roll back the config apply it's attached to."""

    def __init__(self, message: str = "simulated backup push failure") -> None:
        self._message = message

    def push(self, ident: dict, io_config: dict, fingerprint: ConfigFingerprint) -> BackupPushResult:
        return BackupPushResult(ok=False, message=self._message)
