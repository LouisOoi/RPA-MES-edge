"""
AR-08 — per-slave timeout/retry + graceful degradation for a dead RTU
slave. See IO_Config_Execution_Plan.md's AR-08 row: RTU polling didn't
degrade gracefully — every cycle spent the same timeout budget on a dead
slave as on a live one. Covers engine/device_health.py directly,
point_io.read_point's new retry behavior, and the PollEngine integration
(mark-dead after N failures, slower re-probe rate, recovery).
"""
from __future__ import annotations

import pytest
from fake_modbus_client import FakeModbusClient

from engine.device_health import DeviceHealthTracker, resolve_comms
from engine.event_store import fetch_events, init_db
from engine.point_io import read_point
from engine.poll_engine import PollEngine

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


# -- resolve_comms ---------------------------------------------------------

def test_resolve_comms_falls_back_to_bus_serial_for_rtu():
    bus = {"transport": "rtu", "serial": {"timeout_ms": 500, "retries": 1, "backoff_ms": 50}}
    device = {"unit_id": 1, "name": "d1", "type": "remote_io"}
    comms = resolve_comms(bus, device)
    assert comms["timeout_ms"] == 500
    assert comms["retries"] == 1
    assert comms["backoff_ms"] == 50
    assert comms["mark_dead_after_failures"] == 3  # hardcoded default
    assert comms["dead_rescan_ms"] == 5000  # hardcoded default


def test_resolve_comms_per_device_override_wins_over_bus_default():
    bus = {"transport": "rtu", "serial": {"timeout_ms": 500, "retries": 1, "backoff_ms": 50}}
    device = {
        "unit_id": 1, "name": "d1", "type": "remote_io",
        "comms": {"timeout_ms": 200, "mark_dead_after_failures": 5, "dead_rescan_ms": 1000},
    }
    comms = resolve_comms(bus, device)
    assert comms["timeout_ms"] == 200        # overridden
    assert comms["retries"] == 1             # inherited from bus.serial
    assert comms["mark_dead_after_failures"] == 5
    assert comms["dead_rescan_ms"] == 1000


def test_resolve_comms_uses_hardcoded_last_resort_when_nothing_configured():
    bus = {"transport": "rtu"}
    device = {"unit_id": 1, "name": "d1", "type": "remote_io"}
    comms = resolve_comms(bus, device)
    assert comms["timeout_ms"] == 800
    assert comms["retries"] == 2
    assert comms["backoff_ms"] == 100


# -- point_io.read_point retry behavior -------------------------------------

def test_read_point_without_comms_behaves_like_before_ar08():
    client = FakeModbusClient()
    client.fail_addresses.add((1, 10))
    device = {"unit_id": 1, "name": "d1", "type": "remote_io"}
    point = {"id": "p1", "unit_id": 1, "kind": "digital_in", "modbus": {"fn": "read_coils", "address": 10, "count": 1}}
    result = read_point(client, device, point)  # no comms= at all
    assert result.stale is True
    assert len(client.calls) == 1  # single attempt, no retries


def test_read_point_retries_on_failure_then_succeeds_if_it_recovers_mid_retry():
    client = FakeModbusClient()
    client.fail_addresses.add((1, 10))
    device = {"unit_id": 1, "name": "d1", "type": "remote_io"}
    point = {"id": "p1", "unit_id": 1, "kind": "digital_in", "modbus": {"fn": "read_coils", "address": 10, "count": 1}}

    # A real device wouldn't "heal mid-attempt" from our side, but this
    # proves the retry loop actually re-attempts rather than short-
    # circuiting on the first failure.
    class _HealAfterOneFailure(FakeModbusClient):
        def read_coils(self, address, *, count=1, device_id=1):
            resp = super().read_coils(address, count=count, device_id=device_id)
            if len(self.calls) >= 2:
                self.fail_addresses.discard((device_id, address))
            return resp

    client = _HealAfterOneFailure()
    client.fail_addresses.add((1, 10))
    result = read_point(client, device, point, comms={"retries": 2, "backoff_ms": 0})
    assert result.stale is False
    # Attempt 1 fails, attempt 2 also fails (the discard happens AFTER that
    # call's response is already computed), attempt 3 succeeds.
    assert len(client.calls) == 3


def test_read_point_exhausts_all_retries_then_reports_stale():
    client = FakeModbusClient()
    client.fail_addresses.add((1, 10))
    device = {"unit_id": 1, "name": "d1", "type": "remote_io"}
    point = {"id": "p1", "unit_id": 1, "kind": "digital_in", "modbus": {"fn": "read_coils", "address": 10, "count": 1}}
    result = read_point(client, device, point, comms={"retries": 2, "backoff_ms": 0})
    assert result.stale is True
    assert len(client.calls) == 3  # 1 initial + 2 retries


# -- DeviceHealthTracker: cycle-counted mark-dead / rescan / recovery ------

def test_device_stays_probed_every_cycle_while_healthy():
    tracker = DeviceHealthTracker()
    comms = {"mark_dead_after_failures": 3, "dead_rescan_ms": 1000}
    for _ in range(10):
        assert tracker.should_probe_this_cycle(1, comms, poll_interval_ms=100) is True
        tracker.record_result(1, comms, ok=True)


def test_device_marked_dead_after_threshold_consecutive_failures():
    tracker = DeviceHealthTracker()
    comms = {"mark_dead_after_failures": 3, "dead_rescan_ms": 1000}
    assert tracker.is_dead(1) is False
    tracker.record_result(1, comms, ok=False)
    assert tracker.is_dead(1) is False
    tracker.record_result(1, comms, ok=False)
    assert tracker.is_dead(1) is False
    just_died = tracker.record_result(1, comms, ok=False)
    assert just_died is True
    assert tracker.is_dead(1) is True


def test_dead_device_is_only_reprobed_at_the_configured_rescan_rate():
    tracker = DeviceHealthTracker()
    comms = {"mark_dead_after_failures": 1, "dead_rescan_ms": 300}
    poll_interval_ms = 100  # -> rescan every 3 cycles

    tracker.should_probe_this_cycle(1, comms, poll_interval_ms)
    tracker.record_result(1, comms, ok=False)  # marks dead immediately (threshold=1)
    assert tracker.is_dead(1) is True

    # Next two cycles: not yet due for rescan.
    assert tracker.should_probe_this_cycle(1, comms, poll_interval_ms) is False
    assert tracker.should_probe_this_cycle(1, comms, poll_interval_ms) is False
    # Third cycle: due.
    assert tracker.should_probe_this_cycle(1, comms, poll_interval_ms) is True


def test_device_recovers_immediately_on_first_successful_probe():
    tracker = DeviceHealthTracker()
    comms = {"mark_dead_after_failures": 1, "dead_rescan_ms": 300}
    tracker.record_result(1, comms, ok=False)
    assert tracker.is_dead(1) is True

    tracker.record_result(1, comms, ok=True)
    assert tracker.is_dead(1) is False
    # Fully live again -- probed every cycle, not on the rescan schedule.
    assert tracker.should_probe_this_cycle(1, comms, poll_interval_ms=100) is True


# -- PollEngine integration --------------------------------------------------

def _two_device_config(*, mark_dead_after_failures=2, dead_rescan_ms=300, poll_interval_ms=100):
    return {
        "schema_version": 2, "config_version": 1, "updated_at": 0, "updated_by": "test",
        "bus": {
            "transport": "rtu", "poll_interval_ms": poll_interval_ms,
            "serial": {"port": "/dev/ttyUSB0", "baudrate": 9600, "parity": "N", "stopbits": 1, "bytesize": 8},
        },
        "devices": [
            {"unit_id": 1, "name": "Healthy Module", "type": "remote_io"},
            {
                "unit_id": 2, "name": "Flaky Module", "type": "remote_io",
                "comms": {"mark_dead_after_failures": mark_dead_after_failures, "dead_rescan_ms": dead_rescan_ms},
            },
        ],
        "points": [
            {
                "id": "healthy_in", "name": "Healthy In", "unit_id": 1, "kind": "digital_in",
                "modbus": {"fn": "read_coils", "address": 0, "count": 1},
                "scaling": None, "unit": None, "invert": False, "debounce_ms": 0,
            },
            {
                "id": "flaky_in", "name": "Flaky In", "unit_id": 2, "kind": "digital_in",
                "modbus": {"fn": "read_coils", "address": 0, "count": 1},
                "scaling": None, "unit": None, "invert": False, "debounce_ms": 0,
            },
        ],
        "rules": [],
    }


def test_poll_engine_marks_a_dead_slave_and_stops_spending_a_probe_every_cycle(tmp_path):
    io_config = _two_device_config(mark_dead_after_failures=2, dead_rescan_ms=300, poll_interval_ms=100)
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    fake = FakeModbusClient()
    fake.fail_addresses.add((2, 0))  # unit 2 (device_id 2, since no tcp/slave_id override) never answers
    clients = {1: fake, 2: fake}
    engine = PollEngine(io_config, IDENT, db_path, clients=clients)

    engine.run_cycle(now_ms=0)    # failure 1
    engine.run_cycle(now_ms=100)  # failure 2 -> marked dead
    calls_at_mark = len(fake.calls)

    engine.run_cycle(now_ms=200)  # should NOT probe unit 2 (not yet due)
    calls_after_skip_cycle = len(fake.calls)
    assert calls_after_skip_cycle == calls_at_mark + 1  # only unit 1 (healthy) was probed

    events = [e["event_type"] for e in fetch_events(db_path)]
    assert "device_marked_dead" in events

    healthy_result = engine.snapshot.as_dict()["healthy_in"]
    flaky_result = engine.snapshot.as_dict()["flaky_in"]
    assert healthy_result["stale"] is False
    assert flaky_result["stale"] is True


def test_poll_engine_recovers_a_dead_slave_once_it_answers_again(tmp_path):
    io_config = _two_device_config(mark_dead_after_failures=1, dead_rescan_ms=200, poll_interval_ms=100)
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    fake = FakeModbusClient()
    fake.fail_addresses.add((2, 0))
    clients = {1: fake, 2: fake}
    engine = PollEngine(io_config, IDENT, db_path, clients=clients)

    engine.run_cycle(now_ms=0)  # 1 failure -> marked dead immediately (threshold=1)
    events = [e["event_type"] for e in fetch_events(db_path)]
    assert "device_marked_dead" in events

    engine.run_cycle(now_ms=100)  # not yet due for rescan (200ms / 100ms = 2 cycles)

    fake.fail_addresses.discard((2, 0))  # the slave comes back online
    engine.run_cycle(now_ms=200)  # due for rescan this cycle -> should succeed and recover

    events = [e["event_type"] for e in fetch_events(db_path)]
    assert "device_recovered" in events
    assert engine.snapshot.as_dict()["flaky_in"]["stale"] is False
