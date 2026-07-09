"""
OTA updates — Phase 7. "Signed OTA push of app + schema updates: staged
download -> verify signature -> backup current -> migrate config
schema_version -> atomic swap -> health check -> auto-rollback on
failure."

Honest scope, stated up front: this codebase has no real signed binary
app package, no real staged download, and no real device reboot to test a
health check against — there is exactly one schema migration in the whole
project (v1->v2, from Phase 0), and `PollEngine` can only ever run a v2
config; a v1 config was NEVER meant to run live (see io_v1.schema.json).
So this module is split into two composable pieces, each independently
real and testable:

  1. `verify_and_migrate()` — real Ed25519 signature verification (the
     `cryptography` library, not a stub) over a manifest, then the actual
     migration framework from Phase 0 (`migrations.migrate_to_latest`).
     This is the genuinely testable "verify signature -> migrate config
     schema_version" half, and it's exercised against the real v1->v2
     migration, not a pretend one.
  2. `apply_and_reload()` — takes an ALREADY-migrated config and drives it
     through the SAME hot-reload (`PollEngine.reload`) and rollback
     (`PollEngine.rollback_to_lkg`) machinery Phase 4 already built and
     tested. The "health check" is a pluggable callable because there is
     no real health signal available without hardware; the default is a
     concrete, meaningful check (no previously-readable point becomes
     stale after the swap), not a rubber stamp.

A real deployment wires these together with a real download+signature
step in front and a real reboot/service-restart health check behind; this
module does not pretend to be that.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import config_store
from .event_store import log_event
from .ota_state import record_ota_status
from .poll_engine import PollEngine, ReloadResult

try:
    from migrations import MigrationError, migrate_to_latest
except ImportError:  # pragma: no cover - migrations lives at repo root, not in engine/
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from migrations import MigrationError, migrate_to_latest


def canonical_manifest_bytes(manifest: dict) -> bytes:
    """Deterministic byte representation of a manifest for signing/
    verification — sorted keys, no ambiguous whitespace, so the same
    logical manifest always produces the same signed bytes."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(private_key: Ed25519PrivateKey, manifest: dict) -> bytes:
    """Test/tooling helper — the real signing key lives at the release
    pipeline, not on a device, so this exists for building test fixtures
    and reference tooling, not for use on a terminal."""
    return private_key.sign(canonical_manifest_bytes(manifest))


def verify_manifest_signature(public_key: Ed25519PublicKey, manifest: dict, signature: bytes) -> bool:
    try:
        public_key.verify(signature, canonical_manifest_bytes(manifest))
        return True
    except InvalidSignature:
        return False


@dataclass(frozen=True)
class MigrateResult:
    ok: bool
    migrated_config: dict | None = None
    problems: list[str] = field(default_factory=list)


def verify_and_migrate(
    current_io_config: dict, manifest: dict, signature: bytes, *, public_key: Ed25519PublicKey,
) -> MigrateResult:
    """Verify signature, then migrate `current_io_config` forward to
    `manifest["target_schema_version"]`. Never mutates `current_io_config`
    — migrations.migrate_to_latest already returns new dicts."""
    if not verify_manifest_signature(public_key, manifest, signature):
        return MigrateResult(ok=False, problems=["invalid OTA manifest signature"])

    target = manifest.get("target_schema_version")
    current = current_io_config.get("schema_version")
    if target is None:
        return MigrateResult(ok=False, problems=["manifest missing target_schema_version"])
    if target < current:
        return MigrateResult(ok=False, problems=[f"refusing to downgrade schema {current} -> {target}"])

    if target == current:
        return MigrateResult(ok=True, migrated_config=current_io_config)

    # migrate_to_latest walks forward to whatever LATEST_SCHEMA_VERSION
    # this app build supports. If the manifest asks for something further
    # than that, the app itself needs updating first — not something a
    # config migration can paper over.
    try:
        migrated = migrate_to_latest(current_io_config, updated_by="ota")
    except MigrationError as exc:
        return MigrateResult(ok=False, problems=[str(exc)])

    if migrated.get("schema_version") != target:
        return MigrateResult(ok=False, problems=[
            f"migrated to schema_version {migrated.get('schema_version')}, "
            f"but manifest declared target {target} — app/migration mismatch"
        ])
    return MigrateResult(ok=True, migrated_config=migrated)


def default_health_check(poll_engine: PollEngine) -> tuple[bool, str]:
    """Runs one poll cycle on the newly-applied config and fails if ANY
    currently-defined point comes back stale. Meaningful (it genuinely
    catches "the new config can't talk to its declared hardware") without
    pretending to be a real device reboot + service health probe, which
    doesn't exist to check against in this sandbox."""
    results = poll_engine.run_cycle()
    stale_points = [pid for pid, r in results.items() if r.stale]
    if stale_points:
        return False, f"points stale immediately after apply: {stale_points}"
    return True, "ok"


@dataclass(frozen=True)
class OtaResult:
    ok: bool
    rolled_back: bool = False
    config_version: int | None = None
    problems: list[str] = field(default_factory=list)


def apply_and_reload(
    poll_engine: PollEngine,
    migrated_config: dict,
    *,
    io_config_path: str | Path,
    ident: dict,
    db_path: str | Path,
    health_check: Callable[[PollEngine], tuple[bool, str]] | None = None,
    status_path: str | Path | None = None,
) -> OtaResult:
    """Takes an ALREADY-verified-and-migrated config (from
    verify_and_migrate) and does: atomic swap (PollEngine.reload, which
    already backs up the pre-swap config to .lkg + version history) ->
    persist -> health check -> auto-rollback to the exact pre-swap config
    on failure. Every step logs an event; if `status_path` is given, the
    final outcome is also written there for a fast local status read
    (see ota_state.py)."""
    health_check = health_check or default_health_check

    def _finish(result: OtaResult) -> OtaResult:
        if status_path is not None:
            record_ota_status(
                status_path, ok=result.ok, rolled_back=result.rolled_back,
                config_version=result.config_version, problems=result.problems,
            )
        return result

    result = poll_engine.reload(migrated_config)
    if not result.ok:
        log_event(db_path, ident, "ota_apply_rejected", {"problems": result.problems})
        return _finish(OtaResult(ok=False, rolled_back=False, problems=result.problems))

    config_store.atomic_write_json(io_config_path, migrated_config)
    log_event(db_path, ident, "ota_apply_swapped", {"config_version": migrated_config.get("config_version")})

    healthy, message = health_check(poll_engine)
    if healthy:
        log_event(db_path, ident, "ota_apply_healthy", {"message": message})
        return _finish(OtaResult(ok=True, rolled_back=False, config_version=poll_engine.io_config.get("config_version")))

    log_event(db_path, ident, "ota_health_check_failed", {"message": message})
    rollback_result: ReloadResult = poll_engine.rollback_to_lkg()
    if not rollback_result.ok:
        # This is the genuinely bad outcome: health check failed AND we
        # couldn't even get back to the prior config. Surface it loudly.
        log_event(db_path, ident, "ota_rollback_failed", {"problems": rollback_result.problems})
        return _finish(OtaResult(ok=False, rolled_back=False, problems=[message, *rollback_result.problems]))

    config_store.atomic_write_json(io_config_path, poll_engine.io_config)
    log_event(db_path, ident, "ota_rolled_back", {"config_version": poll_engine.io_config.get("config_version")})
    return _finish(OtaResult(
        ok=False, rolled_back=True,
        config_version=poll_engine.io_config.get("config_version"),
        problems=[message],
    ))
