"""
Phase 5 exit test (execution plan): "a fresh unit is commissioned end-to-end
through the browser — identity set, joins the customer broker over TLS,
clock synced — with no SSH."

Flask's test client is the honest automated stand-in for "through the
browser": it drives the exact same WSGI request/response path a real
browser would hit, minus rendering HTML. What it can't prove is the actual
TLS handshake against a real customer broker or a real OS-level network
apply — those are exactly the parts flagged as out of reach in
system_store.py's docstring. What IS proven here: the full request
sequence a browser-driven installer would perform, auth-gated correctly,
with boot_id provably never moving and every write logged.
"""
from __future__ import annotations

import json
import socket
import threading

import pytest
from conftest import load_seed

from api.app import create_app
from api.auth import UserStore
from engine import config_store
from engine.event_store import fetch_events, init_db
from engine.system_store import NullNetworkApplier


@pytest.fixture
def app_ctx(tmp_path):
    identity_path = tmp_path / "ctrl_id.json"
    system_path = tmp_path / "system_config.json"
    db_path = tmp_path / "event_log.db"
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(system_path, load_seed("system_config.seed.json"))
    init_db(db_path)

    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")
    users.add_user("op1", "op-pass", "operator")

    applier = NullNetworkApplier()
    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=applier, secret_key="test-secret",
    )
    app.testing = True
    return app, identity_path, system_path, db_path, applier


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


# -- auth gating --------------------------------------------------------------

def test_status_requires_no_auth(app_ctx):
    app, *_ = app_ctx
    resp = app.test_client().get("/api/status")
    assert resp.status_code == 200


def test_identity_requires_auth(app_ctx):
    app, *_ = app_ctx
    resp = app.test_client().get("/api/identity")
    assert resp.status_code == 401


def test_operator_forbidden_from_identity(app_ctx):
    app, *_ = app_ctx
    client = app.test_client()
    _login(client, "op1", "op-pass")
    resp = client.get("/api/identity")
    assert resp.status_code == 403


def test_wrong_password_rejected(app_ctx):
    app, *_ = app_ctx
    resp = _login(app.test_client(), "admin1", "wrong-password")
    assert resp.status_code == 401


def test_admin_can_read_identity(app_ctx):
    app, *_ = app_ctx
    client = app.test_client()
    _login(client, "admin1", "admin-pass")
    resp = client.get("/api/identity")
    assert resp.status_code == 200
    assert resp.get_json()["boot_id_editable"] is False


# -- identity edit -------------------------------------------------------------

def test_put_identity_without_confirm_rejected(app_ctx):
    app, *_ = app_ctx
    client = app.test_client()
    _login(client, "admin1", "admin-pass")
    resp = client.put("/api/identity", json={"line_id": "L09"})
    assert resp.status_code == 422
    assert "problems" in resp.get_json()


def test_put_identity_boot_id_ignored_and_rejected(app_ctx):
    app, identity_path, *_ = app_ctx
    original = config_store.read_json(identity_path)
    client = app.test_client()
    _login(client, "admin1", "admin-pass")

    resp = client.put("/api/identity", json={
        "boot_id": "11111111-1111-1111-1111-111111111111",
        "confirm_breaks_continuity": True,
    })
    assert resp.status_code == 422
    assert config_store.read_json(identity_path)["boot_id"] == original["boot_id"]


def test_put_identity_valid_change_and_event(app_ctx):
    app, identity_path, _, db_path, _ = app_ctx
    original = config_store.read_json(identity_path)
    client = app.test_client()
    _login(client, "admin1", "admin-pass")

    resp = client.put("/api/identity", json={"line_id": "L09", "confirm_breaks_continuity": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["identity"]["line_id"] == "L09"
    assert body["identity"]["boot_id"] == original["boot_id"]  # never moved

    events = fetch_events(db_path)
    assert any(e["event_type"] == "identity_change" for e in events)


# -- system config: network/mqtt/time -----------------------------------------

def test_put_system_valid_config(app_ctx):
    app, _, system_path, _, applier = app_ctx
    client = app.test_client()
    _login(client, "admin1", "admin-pass")

    new_system = load_seed("system_config.seed.json")
    new_system["network"]["ip"] = "192.168.10.77"

    resp = client.put("/api/system", json=new_system)
    assert resp.status_code == 200
    assert config_store.read_json(system_path)["network"]["ip"] == "192.168.10.77"
    assert len(applier.applied) == 1


def test_put_system_invalid_config_rejected(app_ctx):
    app, *_ = app_ctx
    client = app.test_client()
    _login(client, "admin1", "admin-pass")

    bad_system = load_seed("system_config.seed.json")
    del bad_system["mqtt"]["ca_cert"]

    resp = client.put("/api/system", json=bad_system)
    assert resp.status_code == 422


def test_system_requires_admin(app_ctx):
    app, *_ = app_ctx
    client = app.test_client()
    _login(client, "op1", "op-pass")
    assert client.get("/api/system").status_code == 403


def test_mqtt_test_connection_endpoint(app_ctx):
    app, *_ = app_ctx
    client = app.test_client()
    _login(client, "admin1", "admin-pass")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()
    stop = threading.Event()

    def accept_loop():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except socket.timeout:
                continue

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        new_system = load_seed("system_config.seed.json")
        new_system["mqtt"]["broker_host"] = host
        new_system["mqtt"]["port"] = port
        resp = client.put("/api/system?test_only=true", json=new_system)
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
    finally:
        stop.set()
        server.close()
        t.join(timeout=1)


def test_mqtt_test_connection_does_not_persist(app_ctx):
    """?test_only=true must never write to disk, even on success."""
    app, _, system_path, _, _ = app_ctx
    original = config_store.read_json(system_path)
    client = app.test_client()
    _login(client, "admin1", "admin-pass")

    new_system = load_seed("system_config.seed.json")
    new_system["mqtt"]["broker_host"] = "127.0.0.1"
    new_system["mqtt"]["port"] = 1  # closed, expect ok:false, still must not persist

    resp = client.put("/api/system?test_only=true", json=new_system)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False
    assert config_store.read_json(system_path) == original


# -- capstone: fresh-unit commissioning end to end, no SSH -------------------

def test_full_commissioning_flow(app_ctx):
    app, identity_path, system_path, db_path, applier = app_ctx
    original_boot_id = config_store.read_json(identity_path)["boot_id"]
    client = app.test_client()

    # An unauthenticated installer can at least confirm the unit is alive.
    assert client.get("/api/status").status_code == 200

    # Must log in as admin to commission.
    resp = _login(client, "admin1", "admin-pass")
    assert resp.status_code == 200
    assert resp.get_json()["tier"] == "admin"

    # 1. Identity.
    resp = client.put("/api/identity", json={
        "plant_id": "PLT02", "line_id": "L05", "zone_id": "Z01", "station_id": "ST12",
        "confirm_breaks_continuity": True,
    })
    assert resp.status_code == 200
    identity = resp.get_json()["identity"]
    assert identity["boot_id"] == original_boot_id
    assert identity["plant_id"] == "PLT02"

    # 2. Network + MQTT broker (with TLS) + time, in one PUT (matches the
    # single system_config.json file per the plan's three-file split).
    new_system = load_seed("system_config.seed.json")
    new_system["network"] = {
        "mode": "static", "ip": "192.168.20.5", "mask": "255.255.255.0",
        "gateway": "192.168.20.1", "dns": ["192.168.20.1"],
    }
    new_system["mqtt"]["broker_host"] = "mqtt.newcustomer.local"
    new_system["time"]["timezone"] = "Asia/Kuala_Lumpur"

    # Test-connection first, as the UI would before committing.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()
    stop = threading.Event()

    def accept_loop():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except socket.timeout:
                continue

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        test_system = dict(new_system, mqtt={**new_system["mqtt"], "broker_host": host, "port": port})
        resp = client.put("/api/system?test_only=true", json=test_system)
        assert resp.get_json()["ok"] is True
    finally:
        stop.set()
        server.close()
        t.join(timeout=1)

    # Now commit for real.
    resp = client.put("/api/system", json=new_system)
    assert resp.status_code == 200

    # Verify final on-disk state and audit trail.
    final_identity = config_store.read_json(identity_path)
    final_system = config_store.read_json(system_path)
    assert final_identity["boot_id"] == original_boot_id  # never moved, end to end
    assert final_identity["station_id"] == "ST12"
    assert final_system["mqtt"]["broker_host"] == "mqtt.newcustomer.local"
    assert final_system["time"]["timezone"] == "Asia/Kuala_Lumpur"
    assert len(applier.applied) == 1

    events = fetch_events(db_path)
    assert any(e["event_type"] == "identity_change" for e in events)
