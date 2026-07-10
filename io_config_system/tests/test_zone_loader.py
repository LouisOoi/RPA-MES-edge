"""
engine/zone_loader.py — loading a zone directory (ctrl_id.json,
system_config.json, io_config.json, event_log.db) into a live PollEngine.
Same four-file shape Variant A already uses, laid out once per zone.
"""
from __future__ import annotations

import pytest
from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from engine import config_store
from engine.zone_loader import ZoneDirectoryError, load_all_zones, load_zone_from_directory


def _write_zone_dir(base_dir, name):
    zone_dir = base_dir / name
    zone_dir.mkdir()
    config_store.atomic_write_json(zone_dir / "ctrl_id.json", load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(zone_dir / "system_config.json", load_seed("system_config.seed.json"))
    config_store.atomic_write_json(zone_dir / "io_config.json", load_seed("io_config.seed.v2.golden.json"))
    return zone_dir


def _fake_clients_factory(io_config):
    fake = FakeModbusClient()
    return {d["unit_id"]: fake for d in io_config["devices"]}


def test_load_zone_from_directory_builds_a_working_engine(tmp_path):
    zone_dir = _write_zone_dir(tmp_path, "weld_cell")
    engine, paths = load_zone_from_directory(zone_dir, "weld_cell", clients_factory=_fake_clients_factory)
    assert engine.ident["plant_id"] is not None
    assert paths["io_config_path"] == zone_dir / "io_config.json"
    results = engine.run_cycle(now_ms=0)
    assert len(results) > 0


def test_load_zone_from_directory_raises_on_missing_file(tmp_path):
    zone_dir = tmp_path / "incomplete_zone"
    zone_dir.mkdir()
    config_store.atomic_write_json(zone_dir / "ctrl_id.json", load_seed("ctrl_id.seed.json"))
    # system_config.json and io_config.json deliberately missing.
    with pytest.raises(ZoneDirectoryError):
        load_zone_from_directory(zone_dir, "incomplete_zone", clients_factory=_fake_clients_factory)


def test_load_all_zones_discovers_every_subdirectory(tmp_path):
    zones_root = tmp_path / "zones"
    zones_root.mkdir()
    _write_zone_dir(zones_root, "weld_cell")
    _write_zone_dir(zones_root, "leak_test_rig")
    (zones_root / "not_a_zone.txt").write_text("ignored, not a directory")

    zones = load_all_zones(zones_root, clients_factory=_fake_clients_factory)
    assert set(zones.keys()) == {"weld_cell", "leak_test_rig"}
    for engine, _paths in zones.values():
        assert engine.run_cycle(now_ms=0) is not None


def test_load_all_zones_raises_naming_the_broken_zone(tmp_path):
    zones_root = tmp_path / "zones"
    zones_root.mkdir()
    _write_zone_dir(zones_root, "weld_cell")
    (zones_root / "broken_zone").mkdir()  # no files at all

    with pytest.raises(ZoneDirectoryError, match="broken_zone"):
        load_all_zones(zones_root, clients_factory=_fake_clients_factory)
