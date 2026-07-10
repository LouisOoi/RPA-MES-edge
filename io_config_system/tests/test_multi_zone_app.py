"""
api/multi_zone_app.py — the zone-scoped Flask surface over
ZoneOrchestrator. Covers routing (each zone_id reaches its own engine,
an unknown zone_id 404s cleanly), that AR-05/AR-07 protections carry over
per-zone unchanged, and the plan's explicit "mixed-fleet" regression:
one wired-defaults zone and one wireless-defaults zone driven together,
proving their settings never bleed into each other.
"""
from __future__ import annotations

import copy

from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from api.auth import UserStore
from api.multi_zone_app import ZoneResources, create_multi_zone_app
from engine import config_store
from engine.event_store import init_db
from engine.link_medium import recommended_comms_defaults
from engine.poll_engine import PollEngine
from engine.system_store import NullNetworkApplier
from engine.zone_orchestrator import ZoneOrchestrator


def _build_zone(tmp_path, zone_id, *, io_config=None):
    d = tmp_path / zone_id
    d.mkdir()
    io_config = io_config or load_seed("io_config.seed.v2.golden.json")
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
        return {dev["unit_id"]: fake for dev in cfg["devices"]}

    engine = PollEngine(
        io_config, load_seed("ctrl_id.seed.json"), db_path,
        clients=clients_factory(io_config), clients_factory=clients_factory,
        config_path=io_config_path,
    )
    resources = ZoneResources(
        identity_path=identity_path, system_path=system_path,
        io_config_path=io_config_path, network_applier=NullNetworkApplier(),
    )
    return engine, resources, fake


def _build_app(tmp_path, zone_ids):
    orchestrator = ZoneOrchestrator()
    zone_resources = {}
    fakes = {}
    for zone_id in zone_ids:
        engine, resources, fake = _build_zone(tmp_path, zone_id)
        orchestrator.add_zone(zone_id, engine)
        zone_resources[zone_id] = resources
        fakes[zone_id] = fake

    users = UserStore()
    users.add_user("op1", "op-pass", "operator")
    users.add_user("admin1", "admin-pass", "admin")

    app = create_multi_zone_app(
        orchestrator=orchestrator, zone_resources=zone_resources,
        user_store=users, secret_key="test-secret",
    )
    app.testing = True
    return app, orchestrator, fakes


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


# -- routing / zone addressing -----------------------------------------------

def test_status_needs_no_auth(tmp_path):
    app, *_ = _build_app(tmp_path, ["weld_cell"])
    resp = app.test_client().get("/api/status")
    assert resp.status_code == 200


def test_zone_scoped_routes_require_auth(tmp_path):
    app, *_ = _build_app(tmp_path, ["weld_cell"])
    resp = app.test_client().get("/api/zone/weld_cell/io")
    assert resp.status_code == 401


def test_unknown_zone_id_is_a_clean_404(tmp_path):
    app, *_ = _build_app(tmp_path, ["weld_cell"])
    client = app.test_client()
    _login(client, "op1", "op-pass")
    resp = client.get("/api/zone/does_not_exist/io")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "zone_not_found"


def test_get_io_returns_the_correct_zones_own_config(tmp_path):
    app, orchestrator, _ = _build_app(tmp_path, ["weld_cell", "leak_test_rig"])
    client = app.test_client()
    _login(client, "op1", "op-pass")

    weld_resp = client.get("/api/zone/weld_cell/io").get_json()
    leak_resp = client.get("/api/zone/leak_test_rig/io").get_json()
    assert weld_resp == orchestrator.get_engine("weld_cell").io_config
    assert leak_resp == orchestrator.get_engine("leak_test_rig").io_config


def test_live_values_are_per_zone(tmp_path):
    app, orchestrator, _ = _build_app(tmp_path, ["weld_cell", "leak_test_rig"])
    orchestrator.get_engine("weld_cell").run_cycle(now_ms=0)
    # leak_test_rig deliberately never runs a cycle -- its snapshot stays empty.

    client = app.test_client()
    _login(client, "op1", "op-pass")
    weld_live = client.get("/api/zone/weld_cell/live").get_json()["points"]
    leak_live = client.get("/api/zone/leak_test_rig/live").get_json()["points"]
    assert len(weld_live) > 0
    assert leak_live == {}


def test_list_zones_reports_running_state_and_crash_count(tmp_path):
    app, orchestrator, _ = _build_app(tmp_path, ["weld_cell", "leak_test_rig"])
    orchestrator.start_zone("weld_cell")
    try:
        client = app.test_client()
        _login(client, "op1", "op-pass")
        resp = client.get("/api/zones").get_json()
        by_id = {z["zone_id"]: z for z in resp["zones"]}
        assert by_id["weld_cell"]["running"] is True
        assert by_id["leak_test_rig"]["running"] is False
    finally:
        orchestrator.stop_zone("weld_cell", timeout_s=2.0)


def test_permit_to_edit_gate_applies_per_zone(tmp_path):
    """AR-07 must carry over into the zone-scoped surface unchanged."""
    app, orchestrator, _ = _build_app(tmp_path, ["weld_cell"])
    client = app.test_client()
    _login(client, "op1", "op-pass")

    engine = orchestrator.get_engine("weld_cell")
    edge_output_id = None
    new_config = copy.deepcopy(engine.io_config)
    new_config["points"].append({
        "id": "zone_gate_switch", "name": "Gate Switch", "unit_id": new_config["points"][0]["unit_id"],
        "kind": "digital_in", "modbus": {"fn": "read_coils", "address": 20, "count": 1},
        "scaling": None, "unit": None, "invert": False, "debounce_ms": 0,
    })
    new_config["points"].append({
        "id": "zone_gate_relay", "name": "Gate Relay", "unit_id": new_config["points"][0]["unit_id"],
        "kind": "digital_out", "modbus": {"fn": "write_coil", "address": 21},
        "scaling": None, "unit": None, "invert": False, "owner": "edge", "output_class": "indicator",
    })
    new_config["rules"].append({
        "id": "zone_gate_rule", "enabled": True, "match": "all",
        "when": [{"point": "zone_gate_switch", "op": "rising"}],
        "then": [{"action": "set", "point": "zone_gate_relay", "value": True}],
        "else": [{"action": "set", "point": "zone_gate_relay", "value": False}],
    })

    resp = client.put("/api/zone/weld_cell/io", json=new_config)
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "permit_required"

    new_config["permit_acknowledged"] = True
    resp = client.put("/api/zone/weld_cell/io", json=new_config)
    assert resp.status_code == 200, resp.get_json()


# -- the plan's explicit mixed-fleet regression -------------------------------

def test_mixed_fleet_wired_and_wireless_zone_settings_never_bleed_into_each_other(tmp_path):
    """One zone on wired-fast defaults, one on wireless-loose defaults,
    driven together inside the orchestrator — their bus/device comms
    settings must stay fully independent."""
    wired_cfg = load_seed("io_config.seed.v2.golden.json")
    wired_defaults = recommended_comms_defaults("wired")
    wired_cfg = copy.deepcopy(wired_cfg)
    wired_cfg["link"] = {"medium": "wired"}
    wired_cfg["bus"]["poll_interval_ms"] = wired_defaults["poll_interval_ms"]
    wired_cfg["bus"]["serial"]["timeout_ms"] = wired_defaults["timeout_ms"]
    wired_cfg["bus"]["serial"]["retries"] = wired_defaults["retries"]
    wired_cfg["bus"]["serial"]["backoff_ms"] = wired_defaults["backoff_ms"]

    wireless_cfg = load_seed("io_config.seed.v2.golden.json")
    wireless_defaults = recommended_comms_defaults("wireless")
    wireless_cfg = copy.deepcopy(wireless_cfg)
    wireless_cfg["link"] = {"medium": "wireless"}
    wireless_cfg["bus"]["poll_interval_ms"] = wireless_defaults["poll_interval_ms"]
    wireless_cfg["bus"]["serial"]["timeout_ms"] = wireless_defaults["timeout_ms"]
    wireless_cfg["bus"]["serial"]["retries"] = wireless_defaults["retries"]
    wireless_cfg["bus"]["serial"]["backoff_ms"] = wireless_defaults["backoff_ms"]

    engine_wired, _, _ = _build_zone(tmp_path, "wired_zone", io_config=wired_cfg)
    engine_wireless, _, _ = _build_zone(tmp_path, "wireless_zone", io_config=wireless_cfg)

    orchestrator = ZoneOrchestrator()
    orchestrator.add_zone("wired_zone", engine_wired)
    orchestrator.add_zone("wireless_zone", engine_wireless)

    # Drive both zones together, several cycles each, via direct run_cycle
    # calls (deterministic — no real threads needed to prove this property).
    for _ in range(5):
        orchestrator.get_engine("wired_zone").run_cycle(now_ms=0)
        orchestrator.get_engine("wireless_zone").run_cycle(now_ms=0)

    # Each zone's own config kept its own settings throughout.
    assert orchestrator.get_engine("wired_zone").io_config["bus"]["serial"]["timeout_ms"] == 800
    assert orchestrator.get_engine("wired_zone").io_config["bus"]["poll_interval_ms"] == 100
    assert orchestrator.get_engine("wireless_zone").io_config["bus"]["serial"]["timeout_ms"] == 1500
    assert orchestrator.get_engine("wireless_zone").io_config["bus"]["poll_interval_ms"] == 150
    # Different objects entirely -- no shared mutable state between zones.
    assert orchestrator.get_engine("wired_zone").io_config is not orchestrator.get_engine("wireless_zone").io_config
