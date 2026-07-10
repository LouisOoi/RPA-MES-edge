import copy

from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from engine import config_store
from engine.event_store import init_db
from engine.poll_engine import PollEngine

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


def test_save_list_read_version_roundtrip(tmp_path):
    path = tmp_path / "io_config.json"
    doc1 = {"config_version": 1, "note": "one"}
    doc2 = {"config_version": 2, "note": "two"}

    config_store.save_version(path, doc1)
    config_store.save_version(path, doc2)

    assert config_store.list_versions(path) == [1, 2]
    assert config_store.read_version(path, 1) == doc1
    assert config_store.read_version(path, 2) == doc2


def test_list_versions_empty_when_none_saved(tmp_path):
    path = tmp_path / "io_config.json"
    assert config_store.list_versions(path) == []


def _make_engine(tmp_path, fake, config_path):
    io_config = load_seed("io_config.seed.v2.golden.json")
    config_store.atomic_write_json(config_path, io_config)
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    clients = {d["unit_id"]: fake for d in io_config["devices"]}
    engine = PollEngine(io_config, IDENT, db_path, clients=clients, config_path=config_path)
    return engine, io_config


def test_rollback_reaches_further_back_than_one_step(tmp_path):
    fake = FakeModbusClient()
    config_path = tmp_path / "io_config.json"
    engine, v1 = _make_engine(tmp_path, fake, config_path)

    v2 = copy.deepcopy(v1)
    v2["config_version"] = 2
    engine.reload(v2, clients={d["unit_id"]: fake for d in v2["devices"]})

    v3 = copy.deepcopy(v2)
    v3["config_version"] = 3
    engine.reload(v3, clients={d["unit_id"]: fake for d in v3["devices"]})

    assert engine.io_config["config_version"] == 3
    assert engine.list_config_versions() == [1, 2]  # both superseded versions kept

    result = engine.rollback_to_version(1)
    assert result.ok is True
    assert engine.io_config["config_version"] == 1


def test_rollback_to_missing_version_fails_cleanly(tmp_path):
    fake = FakeModbusClient()
    config_path = tmp_path / "io_config.json"
    engine, _ = _make_engine(tmp_path, fake, config_path)

    result = engine.rollback_to_version(99)
    assert result.ok is False
    assert any("99" in p for p in result.problems)
