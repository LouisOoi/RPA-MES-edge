from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from engine.event_store import fetch_events, init_db
from engine.poll_engine import PollEngine

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


def _make_engine(tmp_path, fake):
    io_config = load_seed("io_config.seed.v2.golden.json")
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    clients = {d["unit_id"]: fake for d in io_config["devices"]}
    engine = PollEngine(io_config, IDENT, db_path, clients=clients)
    return engine, db_path


def test_rejected_when_commissioning_mode_off(tmp_path):
    fake = FakeModbusClient()
    engine, _ = _make_engine(tmp_path, fake)

    result = engine.request_test_write("led_maint", True, confirm=True, now_ms=0)

    assert result.ok is False
    assert "commissioning mode" in result.message
    assert fake.calls == []


def test_rejected_without_confirm(tmp_path):
    fake = FakeModbusClient()
    engine, _ = _make_engine(tmp_path, fake)
    engine.test_write_manager.set_commissioning_mode(True)

    result = engine.request_test_write("led_maint", True, confirm=False, now_ms=0)

    assert result.ok is False
    assert "confirm" in result.message
    assert fake.calls == []


def test_rejected_for_unknown_point(tmp_path):
    fake = FakeModbusClient()
    engine, _ = _make_engine(tmp_path, fake)
    engine.test_write_manager.set_commissioning_mode(True)

    result = engine.request_test_write("does_not_exist", True, confirm=True, now_ms=0)
    assert result.ok is False
    assert "unknown point" in result.message


def test_rejected_for_non_output_point(tmp_path):
    fake = FakeModbusClient()
    engine, _ = _make_engine(tmp_path, fake)
    engine.test_write_manager.set_commissioning_mode(True)

    result = engine.request_test_write("btn_maint", True, confirm=True, now_ms=0)  # digital_in, not out
    assert result.ok is False
    assert "not a digital_out" in result.message


def test_successful_write_auto_reverts_to_safe_state(tmp_path):
    fake = FakeModbusClient()
    engine, db_path = _make_engine(tmp_path, fake)
    engine.test_write_manager.set_commissioning_mode(True)

    result = engine.request_test_write("led_maint", True, confirm=True, timeout_ms=5000, now_ms=0)
    assert result.ok is True
    assert ("write_coil", 1, True, 1) in fake.calls

    engine.run_cycle(now_ms=3000)  # not due yet
    assert ("write_coil", 1, False, 1) not in fake.calls

    engine.run_cycle(now_ms=5500)  # due -> reverts to safe_state (default False)
    assert ("write_coil", 1, False, 1) in fake.calls

    events = fetch_events(db_path)
    assert any(e["event_type"] == "test_write" for e in events)
    assert any(e["event_type"] == "test_write_revert" for e in events)


def test_reverts_to_custom_safe_state(tmp_path):
    fake = FakeModbusClient()
    io_config = load_seed("io_config.seed.v2.golden.json")
    for p in io_config["points"]:
        if p["id"] == "led_maint":
            p["safe_state"] = True
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    clients = {d["unit_id"]: fake for d in io_config["devices"]}
    engine = PollEngine(io_config, IDENT, db_path, clients=clients)
    engine.test_write_manager.set_commissioning_mode(True)

    engine.request_test_write("led_maint", False, confirm=True, timeout_ms=1000, now_ms=0)
    engine.run_cycle(now_ms=1500)

    assert ("write_coil", 1, True, 1) in fake.calls  # reverted to custom safe_state True


def test_disabling_commissioning_mode_does_not_cancel_pending_revert(tmp_path):
    fake = FakeModbusClient()
    engine, _ = _make_engine(tmp_path, fake)
    engine.test_write_manager.set_commissioning_mode(True)

    engine.request_test_write("led_maint", True, confirm=True, timeout_ms=1000, now_ms=0)
    engine.test_write_manager.set_commissioning_mode(False)  # mode off mid-pulse

    engine.run_cycle(now_ms=1500)
    assert ("write_coil", 1, False, 1) in fake.calls  # revert still happens on schedule
