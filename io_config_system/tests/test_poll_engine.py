"""
Phase 2 exit test (execution plan): "side-by-side run against current code
produces identical events for the same bus activity."

Scope note: the reference's modbus_poll.py mixes two things that this
codebase deliberately separates — (a) raw IO (which coil/register, on which
slave, read which way) and (b) business logic (debounce, button->LED,
fault->event). (a) is this phase's job; (b) becomes data-driven rules in
Phase 3. So "identical" here is proven at the IO layer: for the same bus
state, the config-driven engine issues the same Modbus calls (address,
count, slave/device_id) and produces the same raw values the hardcoded
script's direct client calls would. That's the honest scope of what Phase 2
alone can prove — the full end-to-end behavioral match promised by the plan
only closes once Phase 3's rule engine sits on top of this.
"""
from __future__ import annotations

from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from engine.event_store import fetch_events, init_db
from engine.poll_engine import PollEngine

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


def _make_engine(tmp_path, io_config, fake_client):
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    # RTU: one shared client instance across every device, exactly like the
    # real shared ModbusSerialClient.
    clients = {d["unit_id"]: fake_client for d in io_config["devices"]}
    engine = PollEngine(io_config, IDENT, db_path, clients=clients)
    return engine, db_path


def test_read_calls_match_reference_addresses_and_slaves(tmp_path):
    """Reproduces modbus_poll.py's two raw reads:
        client.read_coils(BUTTON_COIL, 1, slave=IO_MODULE_ADDR)   # addr 0, unit 1
        client.read_holding_registers(FAULT_REGISTER, 1, slave=PLC_ADDR)  # addr 100, unit 2
    """
    io_config = load_seed("io_config.seed.v2.golden.json")
    fake = FakeModbusClient()
    engine, _ = _make_engine(tmp_path, io_config, fake)

    engine.run_cycle()

    assert ("read_coils", 0, 1, 1) in fake.calls
    assert ("read_holding_registers", 100, 1, 2) in fake.calls
    # led_maint is write_coil -> never polled on the read path
    assert not any(c[0] == "read_coils" and c[1] == 1 for c in fake.calls)


def test_button_press_value_matches_raw_bit(tmp_path):
    io_config = load_seed("io_config.seed.v2.golden.json")
    fake = FakeModbusClient()
    fake.coils[(1, 0)] = True  # button pressed, unit_id 1 (device_id==unit_id on RTU)
    engine, _ = _make_engine(tmp_path, io_config, fake)

    results = engine.run_cycle()

    assert results["btn_maint"].value is True
    assert results["btn_maint"].stale is False
    snap = engine.snapshot.get("btn_maint")
    assert snap["value"] is True and snap["stale"] is False


def test_fault_register_value_matches_raw_register(tmp_path):
    io_config = load_seed("io_config.seed.v2.golden.json")
    fake = FakeModbusClient()
    fake.registers[(2, 100)] = 7
    engine, _ = _make_engine(tmp_path, io_config, fake)

    results = engine.run_cycle()

    assert results["fault_code"].value == 7
    assert engine.snapshot.get("fault_code")["value"] == 7


def test_write_point_matches_reference_led_write(tmp_path):
    """Reproduces: client.write_coil(LED_COIL, True, slave=IO_MODULE_ADDR)"""
    io_config = load_seed("io_config.seed.v2.golden.json")
    fake = FakeModbusClient()
    engine, _ = _make_engine(tmp_path, io_config, fake)

    result = engine.write_point("led_maint", True)

    assert result.stale is False
    assert ("write_coil", 1, True, 1) in fake.calls
    assert fake.coils[(1, 1)] is True
    assert engine.snapshot.get("led_maint")["value"] is True


def test_bus_read_error_is_stale_not_zero_and_logs_event(tmp_path):
    io_config = load_seed("io_config.seed.v2.golden.json")
    fake = FakeModbusClient()
    fake.fail_addresses.add((2, 100))  # fault register unreadable this cycle
    engine, db_path = _make_engine(tmp_path, io_config, fake)

    results = engine.run_cycle()

    assert results["fault_code"].value is None  # never coerced to 0
    assert results["fault_code"].stale is True
    snap = engine.snapshot.get("fault_code")
    assert snap["stale"] is True and snap["value"] is None

    events = fetch_events(db_path)
    assert any(e["event_type"] == "bus_read_error" for e in events)
    error_event = next(e for e in events if e["event_type"] == "bus_read_error")
    for field in ("plant_id", "line_id", "zone_id", "station_id", "boot_id"):
        assert error_event[field] == IDENT[field]  # all 6 identity fields stamped


def test_stale_recovery_logs_transition_once(tmp_path):
    io_config = load_seed("io_config.seed.v2.golden.json")
    fake = FakeModbusClient()
    fake.fail_addresses.add((2, 100))
    engine, db_path = _make_engine(tmp_path, io_config, fake)

    engine.run_cycle()  # cycle 1: fails -> logs bus_read_error
    engine.run_cycle()  # cycle 2: still failing -> no duplicate log
    fake.fail_addresses.discard((2, 100))
    fake.registers[(2, 100)] = 0
    engine.run_cycle()  # cycle 3: recovers -> logs bus_read_recovered

    events = fetch_events(db_path)
    error_events = [e for e in events if e["event_type"] == "bus_read_error"]
    recovered_events = [e for e in events if e["event_type"] == "bus_read_recovered"]
    assert len(error_events) == 1       # not re-logged every failing cycle
    assert len(recovered_events) == 1


def test_engine_refuses_to_construct_from_invalid_config(tmp_path):
    io_config = load_seed("io_config.seed.v2.golden.json")
    io_config["points"][0]["unit_id"] = 999  # dangling device ref
    fake = FakeModbusClient()
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    import pytest
    from validators import ConfigValidationError
    with pytest.raises(ConfigValidationError):
        PollEngine(io_config, IDENT, db_path, clients={1: fake, 2: fake})
