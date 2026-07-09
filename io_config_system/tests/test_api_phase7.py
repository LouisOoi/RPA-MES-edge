"""
Phase 7 exit test, HTTP layer: push a signed OTA through the actual API
(admin-tier, no SSH), confirm a bad signature is rejected before anything
is touched, and confirm a deliberately failing update auto-rolls-back
through the real endpoint -- with GET /api/ota/status reflecting it.
"""
from __future__ import annotations

import base64
import copy

import pytest
from conftest import load_seed
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fake_modbus_client import FakeModbusClient

from api.app import create_app
from api.auth import UserStore
from engine import config_store, ota
from engine.event_store import init_db
from engine.poll_engine import PollEngine
from engine.system_store import NullNetworkApplier

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


@pytest.fixture
def ota_ctx(tmp_path):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    io_config = load_seed("io_config.seed.v2.golden.json")
    io_config_path = tmp_path / "io_config.json"
    identity_path = tmp_path / "ctrl_id.json"
    system_path = tmp_path / "system_config.json"
    db_path = tmp_path / "event_log.db"
    ota_status_path = tmp_path / "ota_status.json"

    config_store.atomic_write_json(io_config_path, io_config)
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(system_path, load_seed("system_config.seed.json"))
    init_db(db_path)

    fake = FakeModbusClient()

    def clients_factory(cfg):
        return {d["unit_id"]: fake for d in cfg["devices"]}

    poll_engine = PollEngine(
        io_config, IDENT, db_path,
        clients=clients_factory(io_config), clients_factory=clients_factory, config_path=io_config_path,
    )

    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")
    users.add_user("op1", "op-pass", "operator")

    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=NullNetworkApplier(), secret_key="test-secret",
        poll_engine=poll_engine, io_config_path=io_config_path,
        ota_public_key=pub, ota_status_path=ota_status_path,
    )
    app.testing = True
    return app, poll_engine, fake, priv, ota_status_path


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def _signed_body(priv, manifest):
    return {"manifest": manifest, "signature": base64.b64encode(ota.sign_manifest(priv, manifest)).decode()}


def test_ota_apply_requires_admin(ota_ctx):
    app, engine, fake, priv, _ = ota_ctx
    client = app.test_client()
    _login(client, "op1", "op-pass")

    resp = client.post("/api/ota/apply", json=_signed_body(priv, {"target_schema_version": 2}))
    assert resp.status_code == 403


def test_ota_apply_rejects_bad_signature_and_touches_nothing(ota_ctx):
    app, engine, fake, priv, _ = ota_ctx
    original_version = engine.io_config["config_version"]
    client = app.test_client()
    _login(client, "admin1", "admin-pass")

    body = _signed_body(priv, {"target_schema_version": 2})
    body["signature"] = base64.b64encode(b"not-a-real-signature-but-32-bytes").decode()

    resp = client.post("/api/ota/apply", json=body)
    assert resp.status_code == 422
    assert engine.io_config["config_version"] == original_version
    assert fake.calls == []


def test_ota_apply_succeeds_and_status_reflects_it(ota_ctx):
    app, engine, fake, priv, status_path = ota_ctx
    client = app.test_client()
    _login(client, "admin1", "admin-pass")

    resp = client.post("/api/ota/apply", json=_signed_body(priv, {"target_schema_version": 2, "app_version": "1.1.0"}))
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["ok"] is True

    status_resp = client.get("/api/ota/status")
    assert status_resp.get_json()["ok"] is True


def test_ota_apply_forced_health_failure_rolls_back_via_http(ota_ctx, monkeypatch):
    app, engine, fake, priv, status_path = ota_ctx
    client = app.test_client()
    _login(client, "admin1", "admin-pass")
    original_version = engine.io_config["config_version"]

    monkeypatch.setattr(ota, "default_health_check", lambda poll_engine: (False, "forced failure for test"))

    resp = client.post("/api/ota/apply", json=_signed_body(priv, {"target_schema_version": 2}))
    assert resp.status_code == 409
    assert engine.io_config["config_version"] == original_version
    assert config_store.read_json(status_path)["rolled_back"] is True

    status_resp = client.get("/api/ota/status")
    body = status_resp.get_json()
    assert body["rolled_back"] is True
    assert body["config_version"] == original_version


def test_ota_status_before_any_update(ota_ctx):
    app, *_ = ota_ctx
    client = app.test_client()
    _login(client, "admin1", "admin-pass")
    resp = client.get("/api/ota/status")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is None


def test_ota_routes_absent_without_public_key(tmp_path):
    io_config = load_seed("io_config.seed.v2.golden.json")
    io_config_path = tmp_path / "io_config.json"
    identity_path = tmp_path / "ctrl_id.json"
    system_path = tmp_path / "system_config.json"
    db_path = tmp_path / "event_log.db"
    config_store.atomic_write_json(io_config_path, io_config)
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(system_path, load_seed("system_config.seed.json"))
    init_db(db_path)

    fake = FakeModbusClient()
    clients = {d["unit_id"]: fake for d in io_config["devices"]}
    poll_engine = PollEngine(io_config, IDENT, db_path, clients=clients, config_path=io_config_path)

    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")
    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=NullNetworkApplier(), secret_key="test-secret",
        poll_engine=poll_engine, io_config_path=io_config_path,
        # no ota_public_key
    )
    client = app.test_client()
    _login(client, "admin1", "admin-pass")
    assert client.get("/api/ota/status").status_code == 404
