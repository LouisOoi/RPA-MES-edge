"""
AR-02 — watchdog interface tests. See engine/watchdog.py's module docstring:
NullWatchdog is the default/accepted-limitation path, LinuxHardwareWatchdog
is untestable for real (no hardware in this sandbox) but its plumbing
(device path handling, loud failure when the device is missing) is.
"""
from __future__ import annotations

from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from engine.event_store import init_db
from engine.poll_engine import PollEngine
from engine.watchdog import LinuxHardwareWatchdog, NullWatchdog, WatchdogUnavailable

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


class FakeWatchdog:
    """Test double: counts pets/closes instead of touching real hardware."""

    def __init__(self):
        self.pet_count = 0
        self.closed = False

    def pet(self):
        self.pet_count += 1

    def close(self):
        self.closed = True


def _make_engine(tmp_path, watchdog=None):
    io_config = load_seed("io_config.seed.v2.golden.json")
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    fake = FakeModbusClient()
    clients = {d["unit_id"]: fake for d in io_config["devices"]}
    return PollEngine(io_config, IDENT, db_path, clients=clients, watchdog=watchdog)


def test_null_watchdog_is_the_default_and_never_raises():
    wd = NullWatchdog()
    wd.pet()
    wd.pet()
    wd.close()
    wd.close()  # safe to call twice


def test_poll_engine_defaults_to_null_watchdog(tmp_path):
    engine = _make_engine(tmp_path)
    assert isinstance(engine.watchdog, NullWatchdog)
    engine.run_cycle(now_ms=0)  # must not raise with the default watchdog


def test_poll_engine_pets_watchdog_every_cycle(tmp_path):
    fake_wd = FakeWatchdog()
    engine = _make_engine(tmp_path, watchdog=fake_wd)

    engine.run_cycle(now_ms=0)
    engine.run_cycle(now_ms=100)
    engine.run_cycle(now_ms=200)

    assert fake_wd.pet_count == 3


def test_poll_engine_close_closes_the_watchdog(tmp_path):
    fake_wd = FakeWatchdog()
    engine = _make_engine(tmp_path, watchdog=fake_wd)

    engine.close()

    assert fake_wd.closed is True


def test_hardware_watchdog_fails_loudly_when_device_is_missing(tmp_path):
    # No real /dev/watchdog in this sandbox. Opening a bare filename in a
    # writable tmp dir would just CREATE a regular file (no error) — so
    # point at a path whose parent directory doesn't exist, which reliably
    # fails to open regardless of platform, to prove the class fails LOUD
    # (WatchdogUnavailable) rather than silently, per its docstring.
    wd = LinuxHardwareWatchdog(device_path=tmp_path / "no-such-directory" / "watchdog")
    try:
        wd.pet()
        assert False, "expected WatchdogUnavailable"
    except WatchdogUnavailable as exc:
        assert "cannot open hardware watchdog device" in str(exc)
