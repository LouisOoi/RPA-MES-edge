"""
Phase 8 exit test: "a clean-room installer commissions a unit from the
docs alone." There's no live human tester in a sandbox, so this is the
strongest available proxy — a test that executes the EXACT numbered steps
in docs/INSTALLER_GUIDE.md, in order, with the exact JSON bodies shown
there, against a fresh unit. If this test passes, the guide is accurate;
if someone edits app.py and doesn't update the guide, this is what catches
the drift before a real installer does.

Flask's test client stands in for curl/a browser — see test_api_phase5.py
for why that substitution is the honest one available here.
"""
from __future__ import annotations

from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from api.app import create_app
from api.auth import UserStore
from engine import config_store
from engine.event_store import init_db
from engine.poll_engine import PollEngine
from engine.rule_engine import RuleEngine
from engine.system_store import NullNetworkApplier


def _fresh_unit(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    io_config_path = d / "io_config.json"
    identity_path = d / "ctrl_id.json"
    system_path = d / "system_config.json"
    db_path = d / "event_log.db"

    io_config = load_seed("io_config.seed.v2.golden.json")
    config_store.atomic_write_json(io_config_path, io_config)
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(system_path, load_seed("system_config.seed.json"))
    init_db(db_path)

    fake = FakeModbusClient()

    def clients_factory(cfg):
        return {dev["unit_id"]: fake for dev in cfg["devices"]}

    poll_engine = PollEngine(
        io_config, {"plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
                    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"},
        db_path, clients=clients_factory(io_config), clients_factory=clients_factory,
        config_path=io_config_path, rule_engine=RuleEngine(io_config["rules"], {}, db_path),
    )

    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")
    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=NullNetworkApplier(), secret_key="test-secret",
        poll_engine=poll_engine, io_config_path=io_config_path,
    )
    app.testing = True
    return app, poll_engine, fake


def test_installer_guide_end_to_end(tmp_path):
    app, engine, fake = _fresh_unit(tmp_path, "unit1")
    client = app.test_client()

    # -- 0. status, no login -------------------------------------------
    assert client.get("/api/status").get_json() == {"ok": True}

    # -- 1. log in -------------------------------------------------------
    resp = client.post("/api/login", json={"username": "admin1", "password": "admin-pass"})
    assert resp.status_code == 200
    assert resp.get_json()["tier"] == "admin"

    # -- 2. set identity ---------------------------------------------------
    resp = client.put("/api/identity", json={
        "plant_id": "PLT02", "line_id": "L05", "zone_id": "Z01", "station_id": "ST12",
        "confirm_breaks_continuity": True,
    })
    assert resp.status_code == 200, resp.get_json()

    # -- 3. network / mqtt / time, with a test_only check first ----------
    system_body = {
        "network": {"mode": "static", "ip": "192.168.20.5", "mask": "255.255.255.0",
                    "gateway": "192.168.20.1", "dns": ["192.168.20.1"]},
        "mqtt": {"broker_host": "mqtt.customer.local", "port": 8883, "tls": True,
                 "ca_cert": "/etc/certs/ca.crt", "client_cert": "/etc/certs/client.crt",
                 "client_key": "/etc/certs/client.key"},
        "time": {"ntp": ["pool.ntp.org"], "timezone": "Asia/Kuala_Lumpur", "rtc_present": True},
    }
    # test_only against an unreachable host is expected to report ok:false
    # (mqtt.customer.local doesn't exist in this sandbox) -- the guide's
    # point is that the CALL SHAPE works, not that this particular host
    # answers. Confirmed separately in test_api_phase5.py against a real
    # local listener.
    test_resp = client.put("/api/system?test_only=true", json=system_body)
    assert test_resp.status_code == 200
    assert "ok" in test_resp.get_json()

    resp = client.put("/api/system", json=system_body)
    assert resp.status_code == 200, resp.get_json()

    # -- 4. bus scan -------------------------------------------------------
    resp = client.post("/api/bus/scan", json={"transport": "rtu"})
    assert resp.status_code == 200
    found_unit_ids = {hit["unit_id"] for hit in resp.get_json()["found"]}
    assert 1 in found_unit_ids  # the remote IO module is there

    # -- 5. read io, add a point + rule, PUT it back ----------------------
    current = client.get("/api/io").get_json()
    current["points"].append({
        "id": "spare_switch", "name": "Spare Switch", "unit_id": 1, "kind": "digital_in",
        "modbus": {"fn": "read_coils", "address": 2, "count": 1},
        "scaling": None, "unit": None, "invert": False, "debounce_ms": 0,
    })
    current["points"].append({
        "id": "spare_relay", "name": "Spare Relay", "unit_id": 1, "kind": "digital_out",
        "modbus": {"fn": "write_coil", "address": 3},
        "scaling": None, "unit": None, "invert": False,
        "owner": "edge", "output_class": "indicator",
    })
    current["rules"].append({
        "id": "rule_spare", "enabled": True, "match": "all",
        "when": [{"point": "spare_switch", "op": "rising"}],
        "then": [{"action": "set", "point": "spare_relay", "value": True}],
        "else": [{"action": "set", "point": "spare_relay", "value": False}],
    })
    # AR-07: adding rule_spare wires new actuation onto an owner:'edge'
    # output (spare_relay) — the installer acknowledges the resulting
    # output states before this takes effect. See docs/INSTALLER_GUIDE.md
    # step 5.
    current["permit_acknowledged"] = True
    resp = client.put("/api/io", json=current)
    assert resp.status_code == 200, resp.get_json()

    # -- 6. verify it's alive ----------------------------------------------
    engine.run_cycle(now_ms=0)  # represents the real poll loop's background thread ticking
    live = client.get("/api/live").get_json()["points"]
    assert "spare_switch" in live
    assert live["spare_switch"]["stale"] is False

    # -- 7. bench-verify via test write -------------------------------------
    resp = client.post("/api/commissioning-mode", json={"enabled": True})
    assert resp.get_json()["commissioning_mode"] is True

    resp = client.post("/api/test/write", json={
        "point": "spare_relay", "value": True, "confirm": True, "timeout_ms": 5000,
    })
    assert resp.status_code == 200, resp.get_json()
    assert ("write_coil", 3, True, 1) in fake.calls

    # -- 8. export, import onto a second identical unit ---------------------
    exported = client.get("/api/io/export").get_json()
    assert "config_version" not in exported

    app2, engine2, fake2 = _fresh_unit(tmp_path, "unit2")
    client2 = app2.test_client()
    client2.post("/api/login", json={"username": "admin1", "password": "admin-pass"})
    # AR-07: cloning this config onto a fresh unit introduces new rule
    # wiring on an owner:'edge' output (spare_relay) — acknowledge it.
    resp = client2.post("/api/io/import", json={**exported, "permit_acknowledged": True})
    assert resp.status_code == 200, resp.get_json()
    assert any(p["id"] == "spare_switch" for p in engine2.io_config["points"])

    # -- 9. version history + rollback ---------------------------------------
    versions_resp = client.get("/api/config/versions")
    assert versions_resp.status_code == 200
    original_version = versions_resp.get_json()["versions"][0]

    rollback_resp = client.post("/api/config/rollback", json={"version": original_version})
    assert rollback_resp.status_code == 200
    assert not any(p["id"] == "spare_switch" for p in engine.io_config["points"])
