"""
AR-07 — permit-to-edit gate for actuating rule changes. See
IO_Config_Execution_Plan.md's Design notes: non-actuating config keeps
instant hot-reload; a change to any owner:'edge' output's rule wiring
requires explicit acknowledgement before it takes effect.
"""
from __future__ import annotations

import copy

import pytest
from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from api.app import create_app
from api.auth import UserStore
from engine import config_store, permit_to_edit
from engine.event_store import init_db
from engine.poll_engine import PollEngine
from engine.rule_engine import RuleEngine
from engine.system_store import NullNetworkApplier

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


# -- unit-level: engine/permit_to_edit.py ---------------------------------

def test_identical_configs_are_never_actuating():
    cfg = load_seed("io_config.seed.v2.golden.json")
    assert permit_to_edit.is_actuating_change(cfg, copy.deepcopy(cfg)) is False


def test_renaming_a_point_is_not_actuating():
    cfg = load_seed("io_config.seed.v2.golden.json")
    new_cfg = copy.deepcopy(cfg)
    new_cfg["points"][0]["name"] = "Renamed " + new_cfg["points"][0]["name"]
    assert permit_to_edit.is_actuating_change(cfg, new_cfg) is False


def test_adding_a_rule_targeting_an_edge_output_is_actuating():
    cfg = load_seed("io_config.seed.v2.golden.json")
    new_cfg = copy.deepcopy(cfg)
    edge_output = next(p for p in new_cfg["points"] if p.get("kind") == "digital_out" and p.get("owner") == "edge")
    new_cfg["points"].append({
        "id": "new_switch", "name": "New Switch", "unit_id": new_cfg["points"][0]["unit_id"],
        "kind": "digital_in", "modbus": {"fn": "read_coils", "address": 9, "count": 1},
        "scaling": None, "unit": None, "invert": False, "debounce_ms": 0,
    })
    new_cfg["rules"].append({
        "id": "new_rule", "enabled": True, "match": "all",
        "when": [{"point": "new_switch", "op": "rising"}],
        "then": [{"action": "set", "point": edge_output["id"], "value": True}],
        "else": [{"action": "set", "point": edge_output["id"], "value": False}],
    })
    assert permit_to_edit.is_actuating_change(cfg, new_cfg) is True


def test_disabling_a_rule_is_actuating():
    """Disabling a rule that currently drives an edge output changes what
    will actually happen to that output going forward — still in scope."""
    cfg = load_seed("io_config.seed.v2.golden.json")
    new_cfg = copy.deepcopy(cfg)
    rule = next(r for r in new_cfg["rules"] if r.get("enabled", True))
    rule["enabled"] = False
    assert permit_to_edit.is_actuating_change(cfg, new_cfg) is True


def test_changing_a_rule_that_only_targets_a_plc_owned_point_is_not_actuating():
    cfg = load_seed("io_config.seed.v2.golden.json")
    plc_point = {
        "id": "plc_owned_out", "name": "PLC Owned", "unit_id": cfg["points"][0]["unit_id"],
        "kind": "digital_out", "modbus": {"fn": "write_coil", "address": 8},
        "scaling": None, "unit": None, "invert": False,
        "owner": "plc", "output_class": None,
    }
    switch_point = {
        "id": "plc_switch", "name": "PLC Switch", "unit_id": cfg["points"][0]["unit_id"],
        "kind": "digital_in", "modbus": {"fn": "read_coils", "address": 10, "count": 1},
        "scaling": None, "unit": None, "invert": False, "debounce_ms": 0,
    }
    base = copy.deepcopy(cfg)
    base["points"].extend([plc_point, switch_point])
    # AR-03 forbids edge rules from writing plc-owned points, so this
    # scenario is exercised purely at the permit_to_edit projection level
    # (which only inspects owner/action shape, not full validation) rather
    # than through validate_io — proving the gate itself ignores plc-owned
    # targets even though real configs can never reach this state.
    changed = copy.deepcopy(base)
    changed["rules"].append({
        "id": "plc_rule", "enabled": True, "match": "all",
        "when": [{"point": "plc_switch", "op": "rising"}],
        "then": [{"action": "set", "point": "plc_owned_out", "value": True}],
        "else": [{"action": "set", "point": "plc_owned_out", "value": False}],
    })
    assert permit_to_edit.is_actuating_change(base, changed) is False


def test_resulting_output_states_reports_safe_state_and_rule_coverage():
    cfg = load_seed("io_config.seed.v2.golden.json")
    states = permit_to_edit.resulting_output_states(cfg)
    edge_output = next(p for p in cfg["points"] if p.get("kind") == "digital_out" and p.get("owner") == "edge")
    assert edge_output["id"] in states
    assert states[edge_output["id"]]["safe_state"] == edge_output.get("safe_state", False)
    assert states[edge_output["id"]]["output_class"] == edge_output.get("output_class")


# -- API-level: gate blocks PUT /api/io, permit_acknowledged unblocks it --

def _build_unit(tmp_path, name):
    d = tmp_path / name
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
        return {dev["unit_id"]: fake for dev in cfg["devices"]}

    rule_engine = RuleEngine(io_config["rules"], IDENT, db_path)
    poll_engine = PollEngine(
        io_config, IDENT, db_path,
        clients=clients_factory(io_config), clients_factory=clients_factory,
        config_path=io_config_path, rule_engine=rule_engine,
    )

    users = UserStore()
    users.add_user("op1", "op-pass", "operator")

    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=NullNetworkApplier(), secret_key="test-secret",
        poll_engine=poll_engine, io_config_path=io_config_path,
    )
    app.testing = True
    return app, poll_engine


def _new_switch_and_rule(io_config):
    """Adds a brand-new edge-owned output (not one any existing rule
    already writes) plus a new switch/rule pair driving it — an
    unambiguous actuating change with no output-contention side effect
    against whatever the seed config already wires up."""
    io_config = copy.deepcopy(io_config)
    unit_id = io_config["points"][0]["unit_id"]
    io_config["points"].append({
        "id": "gate_switch", "name": "Gate Switch", "unit_id": unit_id,
        "kind": "digital_in", "modbus": {"fn": "read_coils", "address": 11, "count": 1},
        "scaling": None, "unit": None, "invert": False, "debounce_ms": 0,
    })
    io_config["points"].append({
        "id": "gate_relay", "name": "Gate Relay", "unit_id": unit_id,
        "kind": "digital_out", "modbus": {"fn": "write_coil", "address": 12},
        "scaling": None, "unit": None, "invert": False,
        "owner": "edge", "output_class": "indicator",
    })
    io_config["rules"].append({
        "id": "gate_rule", "enabled": True, "match": "all",
        "when": [{"point": "gate_switch", "op": "rising"}],
        "then": [{"action": "set", "point": "gate_relay", "value": True}],
        "else": [{"action": "set", "point": "gate_relay", "value": False}],
    })
    return io_config


def test_put_io_actuating_change_is_rejected_without_acknowledgement(tmp_path):
    app, engine = _build_unit(tmp_path, "unit1")
    client = app.test_client()
    client.post("/api/login", json={"username": "op1", "password": "op-pass"})

    new_config = _new_switch_and_rule(engine.io_config)
    resp = client.put("/api/io", json=new_config)
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"] == "permit_required"
    assert "pending_output_states" in body
    # The rejected reload must never have taken effect.
    assert not any(p["id"] == "gate_switch" for p in engine.io_config["points"])


def test_put_io_actuating_change_applies_once_acknowledged(tmp_path):
    app, engine = _build_unit(tmp_path, "unit1")
    client = app.test_client()
    client.post("/api/login", json={"username": "op1", "password": "op-pass"})

    new_config = _new_switch_and_rule(engine.io_config)
    new_config["permit_acknowledged"] = True
    resp = client.put("/api/io", json=new_config)
    assert resp.status_code == 200, resp.get_json()
    assert any(p["id"] == "gate_switch" for p in engine.io_config["points"])


def test_non_actuating_put_io_never_hits_the_gate(tmp_path):
    app, engine = _build_unit(tmp_path, "unit1")
    client = app.test_client()
    client.post("/api/login", json={"username": "op1", "password": "op-pass"})

    new_config = copy.deepcopy(engine.io_config)
    new_config["points"][0]["name"] = "Renamed Point"
    resp = client.put("/api/io", json=new_config)
    assert resp.status_code == 200, resp.get_json()


def test_rollback_to_lkg_is_exempt_from_the_permit_gate(tmp_path):
    """A rollback restores config that was already running (and already
    acknowledged, if it was ever actuating) — gating it would slow down
    exactly the emergency-recovery path AR-07 must not get in the way of.
    """
    app, engine = _build_unit(tmp_path, "unit1")
    client = app.test_client()
    client.post("/api/login", json={"username": "op1", "password": "op-pass"})

    new_config = _new_switch_and_rule(engine.io_config)
    new_config["permit_acknowledged"] = True
    resp = client.put("/api/io", json=new_config)
    assert resp.status_code == 200, resp.get_json()
    assert any(p["id"] == "gate_switch" for p in engine.io_config["points"])

    resp = client.post("/api/config/rollback", json={})  # rollback_to_lkg (no version given)
    assert resp.status_code == 200, resp.get_json()
    assert not any(p["id"] == "gate_switch" for p in engine.io_config["points"])
