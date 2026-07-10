"""
AR-04 — local NTP default + monotonic-clock interval math. See
IO_Config_Execution_Plan.md's AR-04 row: a backward wall-clock step (NTP
correction) must never delay/skip a scheduled pulse revert, and must be
logged, not silently absorbed.
"""
from __future__ import annotations

import json

from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from engine.event_store import fetch_events, init_db
from engine.poll_engine import PollEngine
from engine.system_store import flag_public_ntp_hosts

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


def _make_engine(tmp_path):
    io_config = load_seed("io_config.seed.v2.golden.json")
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    fake = FakeModbusClient()
    clients = {d["unit_id"]: fake for d in io_config["devices"]}
    engine = PollEngine(io_config, IDENT, db_path, clients=clients)
    return engine, db_path, fake


def test_backward_wall_clock_step_is_logged(tmp_path):
    engine, db_path, _ = _make_engine(tmp_path)

    engine.run_cycle(now_ms=10_000)
    engine.run_cycle(now_ms=4_000)  # wall clock stepped backward 6s (NTP correction)

    events = [e for e in fetch_events(db_path) if e["event_type"] == "clock_step_backward"]
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["previous_now_ms"] == 10_000
    assert payload["new_now_ms"] == 4_000


def test_forward_clock_progression_logs_nothing(tmp_path):
    engine, db_path, _ = _make_engine(tmp_path)

    engine.run_cycle(now_ms=0)
    engine.run_cycle(now_ms=100)
    engine.run_cycle(now_ms=200)

    events = [e for e in fetch_events(db_path) if e["event_type"] == "clock_step_backward"]
    assert events == []


def test_pulse_revert_scheduling_survives_a_backward_wall_clock_step(tmp_path):
    """The actual AR-04 hazard: if revert scheduling used the wall clock,
    a backward step right after scheduling would push the effective
    deadline further away, leaving an output energized far longer than
    configured. Driving monotonic_ms forward normally while now_ms jumps
    backward proves the revert still fires on the monotonic schedule."""
    engine, db_path, fake = _make_engine(tmp_path)
    engine.test_write_manager.set_commissioning_mode(True)

    # Schedule a 500ms test-write revert at monotonic_ms=0.
    engine.request_test_write("led_maint", True, confirm=True, timeout_ms=500, now_ms=0, monotonic_ms=0)

    # Wall clock jumps backward hard (simulating a bad NTP correction);
    # monotonic clock keeps advancing normally past the 500ms deadline.
    engine.run_cycle(now_ms=-999_999, monotonic_ms=600)

    assert ("write_coil", 1, False, 1) in fake.calls  # reverted on schedule anyway


def test_flag_public_ntp_hosts_flags_known_public_pools():
    flagged = flag_public_ntp_hosts(["pool.ntp.org", "ntp.local", "time.google.com"])
    assert flagged == ["pool.ntp.org", "time.google.com"]


def test_flag_public_ntp_hosts_clean_for_local_only():
    assert flag_public_ntp_hosts(["ntp.local", "192.168.10.1"]) == []
