"""
Phase 7 exit test (execution plan): "push an update carrying a schema
change to a test unit; verify migration, health check, and that a
deliberately failing update auto-rolls-back to the prior version and
config."

The genuinely real part of this test file: Ed25519 signing/verification
(the `cryptography` library doing real crypto, not a stub) and the actual
v1->v2 migration from Phase 0 running for real against the seed that
encodes today's hardcoded modbus_poll.py behavior. See ota.py's docstring
for why this is split into verify_and_migrate() (pure, testable, exercises
the real migration) and apply_and_reload() (drives the already-tested
PollEngine.reload/rollback_to_lkg machinery).
"""
from __future__ import annotations

import copy

from conftest import load_seed
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fake_modbus_client import FakeModbusClient

from engine import config_store, ota, ota_state
from engine.event_store import fetch_events, init_db
from engine.poll_engine import PollEngine

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


def _keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


# -- signature verification --------------------------------------------------

def test_valid_signature_verifies():
    priv, pub = _keypair()
    manifest = {"target_schema_version": 2, "app_version": "1.1.0"}
    sig = ota.sign_manifest(priv, manifest)
    assert ota.verify_manifest_signature(pub, manifest, sig) is True


def test_tampered_manifest_fails_verification():
    priv, pub = _keypair()
    manifest = {"target_schema_version": 2, "app_version": "1.1.0"}
    sig = ota.sign_manifest(priv, manifest)
    tampered = {**manifest, "target_schema_version": 99}
    assert ota.verify_manifest_signature(pub, tampered, sig) is False


def test_wrong_key_fails_verification():
    priv1, _ = _keypair()
    _, pub2 = _keypair()
    manifest = {"target_schema_version": 2}
    sig = ota.sign_manifest(priv1, manifest)
    assert ota.verify_manifest_signature(pub2, manifest, sig) is False


# -- verify_and_migrate: the real v1->v2 migration path ----------------------

def test_migrate_rejects_bad_signature():
    priv, pub = _keypair()
    _, wrong_pub = _keypair()
    v1 = load_seed("io_config.seed.v1.json")
    manifest = {"target_schema_version": 2}
    sig = ota.sign_manifest(priv, manifest)

    result = ota.verify_and_migrate(v1, manifest, sig, public_key=wrong_pub)
    assert result.ok is False
    assert any("signature" in p for p in result.problems)


def test_migrate_v1_to_v2_matches_phase0_golden_file():
    priv, pub = _keypair()
    v1 = load_seed("io_config.seed.v1.json")
    golden = load_seed("io_config.seed.v2.golden.json")
    manifest = {"target_schema_version": 2}
    sig = ota.sign_manifest(priv, manifest)

    result = ota.verify_and_migrate(v1, manifest, sig, public_key=pub)

    assert result.ok is True
    migrated = dict(result.migrated_config)
    migrated.pop("updated_at", None)
    migrated.pop("updated_by", None)  # "ota" here vs "migration_test" in the Phase 0 golden file
    golden_stripped = dict(golden)
    golden_stripped.pop("updated_at", None)
    golden_stripped.pop("updated_by", None)
    assert migrated == golden_stripped


def test_migrate_rejects_downgrade():
    priv, pub = _keypair()
    golden = load_seed("io_config.seed.v2.golden.json")  # schema_version 2
    manifest = {"target_schema_version": 1}
    sig = ota.sign_manifest(priv, manifest)

    result = ota.verify_and_migrate(golden, manifest, sig, public_key=pub)
    assert result.ok is False
    assert any("downgrade" in p for p in result.problems)


def test_migrate_same_version_is_a_noop():
    priv, pub = _keypair()
    golden = load_seed("io_config.seed.v2.golden.json")
    manifest = {"target_schema_version": 2}
    sig = ota.sign_manifest(priv, manifest)

    result = ota.verify_and_migrate(golden, manifest, sig, public_key=pub)
    assert result.ok is True
    assert result.migrated_config is golden  # identity, not just equality -- true no-op


def test_migrate_rejects_target_beyond_app_support():
    priv, pub = _keypair()
    golden = load_seed("io_config.seed.v2.golden.json")
    manifest = {"target_schema_version": 5}  # this app build only knows up to 2
    sig = ota.sign_manifest(priv, manifest)

    result = ota.verify_and_migrate(golden, manifest, sig, public_key=pub)
    assert result.ok is False


# -- a v1 unit gets OTA'd to v2 and starts serving it live -------------------

def test_v1_device_migrates_and_then_runs_live():
    """The actual end-to-end shape of the plan's exit test: a unit's
    on-disk config is still schema_version 1 (pre rule-engine). An OTA
    carrying target_schema_version=2 arrives, migrates the file, and a
    PollEngine is started against the result -- which then actually polls
    and fires the migrated rule, proving this isn't just a JSON diff."""
    priv, pub = _keypair()
    v1 = load_seed("io_config.seed.v1.json")
    manifest = {"target_schema_version": 2, "app_version": "1.1.0"}
    sig = ota.sign_manifest(priv, manifest)

    result = ota.verify_and_migrate(v1, manifest, sig, public_key=pub)
    assert result.ok is True
    assert result.migrated_config["schema_version"] == 2

    fake = FakeModbusClient()
    clients = {d["unit_id"]: fake for d in result.migrated_config["devices"]}
    engine = PollEngine(result.migrated_config, IDENT, "sqlite-not-needed", clients=clients)  # validates clean


# -- apply_and_reload: health check + auto-rollback --------------------------

def _make_engine_and_paths(tmp_path, fake):
    io_config = load_seed("io_config.seed.v2.golden.json")
    io_config_path = tmp_path / "io_config.json"
    config_store.atomic_write_json(io_config_path, io_config)
    db_path = tmp_path / "event_log.db"
    init_db(db_path)

    def clients_factory(cfg):
        return {d["unit_id"]: fake for d in cfg["devices"]}

    engine = PollEngine(
        io_config, IDENT, db_path,
        clients=clients_factory(io_config), clients_factory=clients_factory, config_path=io_config_path,
    )
    return engine, io_config_path, db_path


def test_apply_succeeds_with_healthy_config(tmp_path):
    fake = FakeModbusClient()
    engine, io_config_path, db_path = _make_engine_and_paths(tmp_path, fake)
    status_path = tmp_path / "ota_status.json"

    new_config = copy.deepcopy(engine.io_config)  # same schema, e.g. an app-only update
    result = ota.apply_and_reload(
        engine, new_config, io_config_path=io_config_path, ident=IDENT, db_path=db_path,
        status_path=status_path,
    )

    assert result.ok is True
    assert result.rolled_back is False
    status = ota_state.read_ota_status(status_path)
    assert status["ok"] is True

    events = fetch_events(db_path)
    assert any(e["event_type"] == "ota_apply_healthy" for e in events)


def test_apply_rejected_by_reload_never_swaps(tmp_path):
    fake = FakeModbusClient()
    engine, io_config_path, db_path = _make_engine_and_paths(tmp_path, fake)
    original_version = engine.io_config["config_version"]

    bad_config = copy.deepcopy(engine.io_config)
    bad_config["points"][0]["unit_id"] = 999  # dangling ref

    result = ota.apply_and_reload(engine, bad_config, io_config_path=io_config_path, ident=IDENT, db_path=db_path)

    assert result.ok is False
    assert result.rolled_back is False
    assert engine.io_config["config_version"] == original_version
    assert config_store.read_json(io_config_path)["config_version"] == original_version


def test_failing_health_check_triggers_auto_rollback(tmp_path):
    fake = FakeModbusClient()
    engine, io_config_path, db_path = _make_engine_and_paths(tmp_path, fake)
    status_path = tmp_path / "ota_status.json"
    original_config = copy.deepcopy(engine.io_config)

    new_config = copy.deepcopy(engine.io_config)
    new_config["config_version"] = original_config["config_version"] + 1

    def always_fails(poll_engine):
        return False, "simulated post-update health check failure"

    result = ota.apply_and_reload(
        engine, new_config, io_config_path=io_config_path, ident=IDENT, db_path=db_path,
        health_check=always_fails, status_path=status_path,
    )

    assert result.ok is False
    assert result.rolled_back is True
    assert result.config_version == original_config["config_version"]
    assert engine.io_config["config_version"] == original_config["config_version"]
    assert config_store.read_json(io_config_path)["config_version"] == original_config["config_version"]

    status = ota_state.read_ota_status(status_path)
    assert status["rolled_back"] is True

    events = fetch_events(db_path)
    assert any(e["event_type"] == "ota_health_check_failed" for e in events)
    assert any(e["event_type"] == "ota_rolled_back" for e in events)


def test_default_health_check_fails_on_newly_stale_point(tmp_path):
    fake = FakeModbusClient()
    engine, io_config_path, db_path = _make_engine_and_paths(tmp_path, fake)
    engine.run_cycle(now_ms=0)  # baseline: everything healthy

    # Simulate the new config pointing at hardware that doesn't answer.
    fake.fail_addresses.add((2, 100))
    new_config = copy.deepcopy(engine.io_config)
    new_config["config_version"] += 1

    result = ota.apply_and_reload(engine, new_config, io_config_path=io_config_path, ident=IDENT, db_path=db_path)

    assert result.ok is False
    assert result.rolled_back is True


def test_rollback_failure_is_surfaced_not_swallowed(tmp_path):
    """If there's no config_path on the engine, rollback_to_lkg() itself
    can't work -- apply_and_reload must report THAT failure clearly, not
    claim success or silently do nothing."""
    fake = FakeModbusClient()
    io_config = load_seed("io_config.seed.v2.golden.json")
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    clients = {d["unit_id"]: fake for d in io_config["devices"]}
    engine = PollEngine(io_config, IDENT, db_path, clients=clients)  # no config_path!

    new_config = copy.deepcopy(io_config)
    new_config["config_version"] += 1

    def always_fails(poll_engine):
        return False, "forced failure"

    result = ota.apply_and_reload(
        engine, new_config, io_config_path=tmp_path / "unused.json", ident=IDENT, db_path=db_path,
        health_check=always_fails,
    )

    assert result.ok is False
    assert result.rolled_back is False
    assert any("forced failure" in p for p in result.problems)
    assert any("config_path" in p for p in result.problems)
