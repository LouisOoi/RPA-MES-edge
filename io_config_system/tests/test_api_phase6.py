"""
Phase 6 exit test (execution plan): "a non-programmer adds a digital
point, links it to a relay, verifies it on the bench, exports the config,
and imports it onto a second unit."

Flask's test client stands in for the browser (see test_api_phase5.py's
note on this). "Verifies it on the bench" is done via /api/test/write
under commissioning mode (the actual bench-verification tool this phase
built), not by reading datasheets. Between HTTP calls, the test calls
poll_engine.run_cycle() directly to represent time passing on the real
poll loop, which in production runs in a background thread this test
harness doesn't spin up.
"""
from __future__ import annotations

import copy

import pytest
from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from api.app import create_app
from api.auth import UserStore
from engine import config_store
from engine.event_store import init_db
from engine.poll_engine import PollEngine
from engine.rule_engine import RuleEngine
from engine.system_store import NullNetworkApplier

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


def _build_unit(tmp_path, name, *, io_config=None):
    """Builds one fully wired terminal: PollEngine + Flask app sharing a
    fake Modbus client, matching how the real web app and poll engine
    share config/state within one physical unit."""
    d = tmp_path / name
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

    rule_engine = RuleEngine(io_config["rules"], IDENT, db_path)
    poll_engine = PollEngine(
        io_config, IDENT, db_path,
        clients=clients_factory(io_config), clients_factory=clients_factory,
        config_path=io_config_path, rule_engine=rule_engine,
    )

    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")
    users.add_user("op1", "op-pass", "operator")

    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=NullNetworkApplier(), secret_key="test-secret",
        poll_engine=poll_engine, io_config_path=io_config_path,
    )
    app.testing = True
    return app, poll_engine, fake, io_config_path


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def test_full_bench_workflow_add_point_verify_export_import(tmp_path):
    app1, engine1, fake1, path1 = _build_unit(tmp_path, "unit1")
    client1 = app1.test_client()
    _login(client1, "op1", "op-pass")

    # -- add a new digital_in + digital_out pair, linked by a rule -------
    new_config = copy.deepcopy(engine1.io_config)
    new_config["points"].append({
        "id": "spare_switch", "name": "Spare Switch", "unit_id": 1, "kind": "digital_in",
        "modbus": {"fn": "read_coils", "address": 2, "count": 1},
        "scaling": None, "unit": None, "invert": False, "debounce_ms": 0,
    })
    new_config["points"].append({
        "id": "spare_relay", "name": "Spare Relay", "unit_id": 1, "kind": "digital_out",
        "modbus": {"fn": "write_coil", "address": 3},
        "scaling": None, "unit": None, "invert": False,
        "owner": "edge", "output_class": "indicator",
    })
    new_config["rules"].append({
        "id": "rule_spare", "enabled": True, "match": "all",
        "when": [{"point": "spare_switch", "op": "rising"}],
        "then": [{"action": "set", "point": "spare_relay", "value": True}],
        "else": [{"action": "set", "point": "spare_relay", "value": False}],
    })

    # AR-07: this adds new rule wiring driving an owner:'edge' output, so
    # it's an actuating change — the bench operator acknowledges it here.
    new_config["permit_acknowledged"] = True
    resp = client1.put("/api/io", json=new_config)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["config_version"] == engine1.io_config["config_version"]

    # -- verify it shows up live -----------------------------------------
    engine1.run_cycle(now_ms=0)
    live = client1.get("/api/live").get_json()["points"]
    assert "spare_switch" in live
    assert live["spare_switch"]["value"] is False

    # -- bench-verify the relay via Test Write ----------------------------
    admin_client1 = app1.test_client()
    _login(admin_client1, "admin1", "admin-pass")
    resp = admin_client1.post("/api/commissioning-mode", json={"enabled": True})
    assert resp.get_json()["commissioning_mode"] is True

    # The HTTP endpoint has no way for a caller to inject fake time (nor
    # should it), so it schedules the revert against real wall-clock time.
    # Use a short real timeout here rather than mixing that with the fake
    # `now_ms` used elsewhere in this test suite.
    resp = client1.post("/api/test/write", json={
        "point": "spare_relay", "value": True, "confirm": True, "timeout_ms": 50,
    })
    assert resp.status_code == 200, resp.get_json()
    assert ("write_coil", 3, True, 1) in fake1.calls

    import time
    time.sleep(0.1)
    engine1.run_cycle()  # real wall-clock time -- the 50ms deadline has passed
    assert ("write_coil", 3, False, 1) in fake1.calls  # auto-reverted to safe_state

    # -- export ------------------------------------------------------------
    exported = client1.get("/api/io/export").get_json()
    assert "config_version" not in exported
    assert any(p["id"] == "spare_switch" for p in exported["points"])

    # -- import onto a second, identical unit ------------------------------
    app2, engine2, fake2, path2 = _build_unit(tmp_path, "unit2")
    client2 = app2.test_client()
    _login(client2, "op1", "op-pass")

    # AR-07: same reasoning as the PUT above — this import introduces new
    # rule wiring on unit2's owner:'edge' spare_relay.
    resp = client2.post("/api/io/import", json={**exported, "permit_acknowledged": True})
    assert resp.status_code == 200, resp.get_json()

    assert any(p["id"] == "spare_switch" for p in engine2.io_config["points"])
    assert config_store.read_json(path2)["points"] == engine2.io_config["points"]

    # The second unit's rule fires independently, proving the imported
    # config is actually live there, not just stored. A rising edge needs
    # a witnessed prior False first (same rule as everywhere else in this
    # codebase — no edge fires on the very first cycle a point is seen).
    engine2.run_cycle(now_ms=0)
    fake2.coils[(1, 2)] = True
    engine2.run_cycle(now_ms=100)
    assert ("write_coil", 3, True, 1) in fake2.calls


def test_import_rejects_malformed_export_payload(tmp_path):
    app1, engine1, fake1, path1 = _build_unit(tmp_path, "unit1")
    client1 = app1.test_client()
    _login(client1, "op1", "op-pass")

    bad_payload = {**engine1.io_config}  # still has config_version -- not a real export
    resp = client1.post("/api/io/import", json=bad_payload)
    assert resp.status_code == 422


def test_test_write_requires_commissioning_mode_over_http(tmp_path):
    app1, engine1, fake1, path1 = _build_unit(tmp_path, "unit1")
    client1 = app1.test_client()
    _login(client1, "op1", "op-pass")

    resp = client1.post("/api/test/write", json={"point": "led_maint", "value": True, "confirm": True})
    assert resp.status_code == 409


def test_bus_scan_endpoint(tmp_path):
    app1, engine1, fake1, path1 = _build_unit(tmp_path, "unit1")
    client1 = app1.test_client()
    _login(client1, "op1", "op-pass")

    resp = client1.post("/api/bus/scan", json={"transport": "rtu"})
    assert resp.status_code == 200
    found = resp.get_json()["found"]
    assert any(hit["unit_id"] == 1 for hit in found)  # unit 1 always answers the fake client


def test_config_versions_and_rollback_endpoints(tmp_path):
    app1, engine1, fake1, path1 = _build_unit(tmp_path, "unit1")
    client1 = app1.test_client()
    _login(client1, "op1", "op-pass")

    new_config = copy.deepcopy(engine1.io_config)
    new_config["points"][0]["debounce_ms"] = 999
    client1.put("/api/io", json=new_config)

    resp = client1.get("/api/config/versions")
    assert resp.status_code == 200
    assert 1 in resp.get_json()["versions"]

    resp = client1.post("/api/config/rollback", json={"version": 1})
    assert resp.status_code == 200
    assert engine1.io_config["config_version"] == 1
