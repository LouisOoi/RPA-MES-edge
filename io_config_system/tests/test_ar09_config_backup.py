"""
AR-09 — central config backup + drift reporting (minimal). See
IO_Config_Execution_Plan.md's amended "Where the UI runs" row: every
successful config apply pushes a non-secret copy to a central backup
store, one-way and best-effort, never as a control dependency. This repo
is the device side only — there is no central component to push to (see
engine/config_backup.py's module docstring) — so these tests cover the
fingerprint logic, the Null default's honest recording, the best-effort
contract (a failing/raising backup client must never affect the reload
it's attached to), and that both initial boot and every reload trigger a
push attempt.
"""
from __future__ import annotations

import copy

import pytest
from conftest import load_seed

from engine.config_backup import (
    AlwaysFailBackupClient,
    BackupPushResult,
    NullConfigBackupClient,
    compute_config_fingerprint,
)
from engine.event_store import fetch_events, init_db
from engine.poll_engine import PollEngine
from fake_modbus_client import FakeModbusClient

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


def _engine(tmp_path, io_config, *, backup_client=None):
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    fake = FakeModbusClient()
    clients = {d["unit_id"]: fake for d in io_config["devices"]}
    return PollEngine(io_config, IDENT, db_path, clients=clients, backup_client=backup_client), db_path


# -- fingerprint -------------------------------------------------------------

def test_fingerprint_is_stable_for_identical_configs():
    cfg = load_seed("io_config.seed.v2.golden.json")
    fp1 = compute_config_fingerprint(cfg, now_ms=0)
    fp2 = compute_config_fingerprint(copy.deepcopy(cfg), now_ms=1000)
    assert fp1.content_hash == fp2.content_hash
    assert fp1.config_version == fp2.config_version == cfg["config_version"]


def test_fingerprint_differs_when_content_differs_even_if_version_is_the_same():
    """The actual point of hashing content, not just trusting
    config_version: two documents claiming the same version number but
    with different content must be distinguishable as drifted."""
    cfg = load_seed("io_config.seed.v2.golden.json")
    drifted = copy.deepcopy(cfg)
    drifted["points"][0]["name"] = "Manually Edited On Device, Never Reported"
    fp_original = compute_config_fingerprint(cfg)
    fp_drifted = compute_config_fingerprint(drifted)
    assert fp_original.config_version == fp_drifted.config_version
    assert fp_original.content_hash != fp_drifted.content_hash


# -- NullConfigBackupClient --------------------------------------------------

def test_null_backup_client_records_but_sends_nothing():
    client = NullConfigBackupClient()
    ident = {"plant_id": "P", "line_id": "L", "zone_id": "Z", "station_id": "S", "boot_id": "B"}
    cfg = load_seed("io_config.seed.v2.golden.json")
    fingerprint = compute_config_fingerprint(cfg)
    result = client.push(ident, cfg, fingerprint)
    assert result.ok is True
    assert len(client.pushed) == 1
    pushed_ident, pushed_cfg, pushed_fp = client.pushed[0]
    assert pushed_ident == ident
    assert pushed_fp == fingerprint


# -- PollEngine integration ---------------------------------------------------

def test_initial_boot_config_is_pushed_to_the_backup_client(tmp_path):
    cfg = load_seed("io_config.seed.v2.golden.json")
    backup = NullConfigBackupClient()
    engine, db_path = _engine(tmp_path, cfg, backup_client=backup)
    assert len(backup.pushed) == 1
    assert backup.pushed[0][2].config_version == cfg["config_version"]


def test_every_successful_reload_triggers_another_push(tmp_path):
    cfg = load_seed("io_config.seed.v2.golden.json")
    backup = NullConfigBackupClient()
    engine, db_path = _engine(tmp_path, cfg, backup_client=backup)
    assert len(backup.pushed) == 1

    new_cfg = copy.deepcopy(engine.io_config)
    new_cfg["config_version"] += 1
    new_cfg["points"][0]["name"] = "Renamed"
    result = engine.reload(new_cfg)
    assert result.ok, result.problems
    assert len(backup.pushed) == 2
    assert backup.pushed[1][2].config_version == new_cfg["config_version"]


def test_backup_defaults_to_null_client_when_none_given(tmp_path):
    cfg = load_seed("io_config.seed.v2.golden.json")
    engine, db_path = _engine(tmp_path, cfg)  # backup_client=None
    assert isinstance(engine.backup_client, NullConfigBackupClient)
    assert len(engine.backup_client.pushed) == 1


def test_a_failing_backup_push_is_logged_but_never_blocks_the_reload(tmp_path):
    cfg = load_seed("io_config.seed.v2.golden.json")
    backup = AlwaysFailBackupClient()
    engine, db_path = _engine(tmp_path, cfg, backup_client=backup)

    events = [e["event_type"] for e in fetch_events(db_path)]
    assert "config_backup_failed" in events  # from the initial boot push

    new_cfg = copy.deepcopy(engine.io_config)
    new_cfg["config_version"] += 1
    new_cfg["points"][0]["name"] = "Renamed Despite Backup Failing"
    result = engine.reload(new_cfg)
    assert result.ok, result.problems  # the reload itself still succeeded
    assert engine.io_config["points"][0]["name"] == "Renamed Despite Backup Failing"

    events = [e["event_type"] for e in fetch_events(db_path)]
    assert events.count("config_backup_failed") == 2  # boot + this reload


def test_a_raising_backup_client_is_caught_and_never_propagates(tmp_path):
    class _ExplodingBackupClient:
        def push(self, ident, io_config, fingerprint):
            raise ConnectionError("central backup host unreachable")

    cfg = load_seed("io_config.seed.v2.golden.json")
    # Must not raise during construction (initial boot push).
    engine, db_path = _engine(tmp_path, cfg, backup_client=_ExplodingBackupClient())

    events = [e["event_type"] for e in fetch_events(db_path)]
    assert "config_backup_failed" in events

    new_cfg = copy.deepcopy(engine.io_config)
    new_cfg["config_version"] += 1
    new_cfg["points"][0]["name"] = "Still Works"
    result = engine.reload(new_cfg)  # must not raise either
    assert result.ok, result.problems
