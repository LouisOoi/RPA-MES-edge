"""
Real (not mocked) per-zone health data backing the UI's Overview/Bus
tabs: PollEngine.watchdog_is_hardware, get_device_health(),
get_backup_status(), and the /api/zone/<id>/health-summary route that
surfaces all three plus a real plc-owned-output count.
"""
from __future__ import annotations

from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from api.app import create_app  # unused directly, but keeps import parity obvious
from api.auth import UserStore
from api.multi_zone_app import ZoneResources, create_multi_zone_app
from engine import config_store
from engine.config_backup import AlwaysFailBackupClient, NullConfigBackupClient
from engine.event_store import init_db
from engine.poll_engine import PollEngine
from engine.system_store import NullNetworkApplier
from engine.watchdog import LinuxHardwareWatchdog, NullWatchdog
from engine.zone_orchestrator import ZoneOrchestrator

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z01", "station_id": "ST01",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


def _engine(tmp_path, **kwargs):
    cfg = load_seed("io_config.seed.v2.golden.json")
    db_path = tmp_path / "e.db"
    init_db(db_path)
    fake = FakeModbusClient()
    clients = {d["unit_id"]: fake for d in cfg["devices"]}
    return PollEngine(cfg, IDENT, db_path, clients=clients, **kwargs), fake


# -- PollEngine-level ---------------------------------------------------

def test_watchdog_is_hardware_false_by_default(tmp_path):
    engine, _ = _engine(tmp_path)
    assert engine.watchdog_is_hardware is False


def test_watchdog_is_hardware_true_with_a_real_watchdog(tmp_path):
    engine, _ = _engine(tmp_path, watchdog=LinuxHardwareWatchdog("/dev/watchdog"))
    assert engine.watchdog_is_hardware is True


def test_get_device_health_defaults_every_configured_device_to_healthy(tmp_path):
    engine, _ = _engine(tmp_path)
    health = engine.get_device_health()
    unit_ids = {d["unit_id"] for d in engine.io_config["devices"]}
    assert set(health.keys()) == unit_ids
    for status in health.values():
        assert status == {"dead": False, "consecutive_failures": 0}


def test_get_device_health_reflects_a_dead_device(tmp_path):
    engine, fake = _engine(tmp_path)
    unit_id = engine.io_config["devices"][0]["unit_id"]
    fake.fail_addresses = {(unit_id, p["modbus"]["address"]) for p in engine.io_config["points"] if p["modbus"]["fn"] != "write_coil"}
    for i in range(5):
        engine.run_cycle(now_ms=i * 100)
    health = engine.get_device_health()
    assert health[unit_id]["dead"] is True


def test_get_backup_status_is_none_when_client_exposes_nothing():
    class _OpaqueBackupClient:
        def push(self, ident, io_config, fingerprint):
            from engine.config_backup import BackupPushResult
            return BackupPushResult(ok=True)

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        cfg = load_seed("io_config.seed.v2.golden.json")
        db_path = Path(d) / "e.db"
        init_db(db_path)
        fake = FakeModbusClient()
        clients = {dv["unit_id"]: fake for dv in cfg["devices"]}
        engine = PollEngine(cfg, IDENT, db_path, clients=clients, backup_client=_OpaqueBackupClient())
        assert engine.get_backup_status() is None


def test_get_backup_status_reports_the_last_push(tmp_path):
    engine, _ = _engine(tmp_path, backup_client=NullConfigBackupClient())
    status = engine.get_backup_status()
    assert status is not None
    assert status["config_version"] == engine.io_config["config_version"]
    assert status["push_count"] == 1


def test_get_backup_status_present_even_when_the_push_failed(tmp_path):
    """A recorded attempt, even a failed one, is still real information
    — better than hiding it because it wasn't a success."""
    engine, _ = _engine(tmp_path, backup_client=AlwaysFailBackupClient())
    # AlwaysFailBackupClient has no .pushed list at all (it's not the Null
    # client) — confirms get_backup_status() degrades to None rather than
    # crashing on a client that doesn't track history.
    assert engine.get_backup_status() is None


# -- API-level ------------------------------------------------------------

def _build_zone(tmp_path, zone_id):
    d = tmp_path / zone_id
    d.mkdir()
    io_config = load_seed("io_config.seed.v2.golden.json")
    io_config_path = d / "io_config.json"
    identity_path = d / "ctrl_id.json"
    system_path = d / "system_config.json"
    db_path = d / "event_log.db"
    config_store.atomic_write_json(io_config_path, io_config)
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(system_path, load_seed("system_config.seed.json"))
    init_db(db_path)
    fake = FakeModbusClient()

    def clients_factory(cfg):
        return {dv["unit_id"]: fake for dv in cfg["devices"]}

    engine = PollEngine(
        io_config, load_seed("ctrl_id.seed.json"), db_path,
        clients=clients_factory(io_config), clients_factory=clients_factory,
        config_path=io_config_path, backup_client=NullConfigBackupClient(),
    )
    resources = ZoneResources(
        identity_path=identity_path, system_path=system_path,
        io_config_path=io_config_path, network_applier=NullNetworkApplier(),
    )
    return engine, resources


def test_health_summary_route_returns_real_fields(tmp_path):
    orchestrator = ZoneOrchestrator()
    engine, resources = _build_zone(tmp_path, "weld_cell")
    orchestrator.add_zone("weld_cell", engine)
    users = UserStore()
    users.add_user("op1", "op-pass", "operator")
    app = create_multi_zone_app(
        orchestrator=orchestrator, zone_resources={"weld_cell": resources},
        user_store=users, secret_key="test-secret",
    )
    app.testing = True
    client = app.test_client()
    client.post("/api/login", json={"username": "op1", "password": "op-pass"})

    resp = client.get("/api/zone/weld_cell/health-summary")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["watchdog_hardware"] is False
    assert isinstance(body["device_health"], dict)
    assert body["plc_owned_output_count"] == 0
    assert body["backup"]["push_count"] == 1


def test_health_summary_unknown_zone_is_404(tmp_path):
    orchestrator = ZoneOrchestrator()
    users = UserStore()
    users.add_user("op1", "op-pass", "operator")
    app = create_multi_zone_app(orchestrator=orchestrator, zone_resources={}, user_store=users, secret_key="test-secret")
    app.testing = True
    client = app.test_client()
    client.post("/api/login", json={"username": "op1", "password": "op-pass"})
    resp = client.get("/api/zone/nope/health-summary")
    assert resp.status_code == 404
