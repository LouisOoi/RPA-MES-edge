"""
AR-05 — real per-user accounts + TLS + lockout. See
IO_Config_Execution_Plan.md's AR-05 row and the amended Auth row: the
two-shared-password model is gone, repeated failed logins must lock the
account out (not just fail forever), and the config UI must be able to
reject cleartext transport when a reverse proxy is in front of it.
"""
from __future__ import annotations

import pytest
from conftest import load_seed

from api.app import create_app
from api.auth import AccountLocked, UserStore
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

    users = UserStore(max_failed_attempts=3, lockout_seconds=60)
    users.add_user("admin1", "admin-pass", "admin")

    applier = NullNetworkApplier()
    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=applier, secret_key="test-secret",
    )
    app.testing = True
    return app, db_path


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


# -- UserStore unit-level lockout behavior -------------------------------

def test_verify_returns_tier_on_correct_password():
    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")
    assert users.verify("admin1", "admin-pass") == "admin"


def test_verify_returns_none_on_unknown_user_or_wrong_password():
    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")
    assert users.verify("nobody", "whatever") is None
    assert users.verify("admin1", "wrong-pass") is None


def test_account_locks_after_max_failed_attempts():
    users = UserStore(max_failed_attempts=3, lockout_seconds=60)
    users.add_user("admin1", "admin-pass", "admin")

    assert users.verify("admin1", "wrong", now=0) is None
    assert users.verify("admin1", "wrong", now=1) is None
    assert users.verify("admin1", "wrong", now=2) is None  # 3rd failure trips the lock

    with pytest.raises(AccountLocked):
        users.verify("admin1", "admin-pass", now=3)  # correct password, still locked


def test_account_unlocks_after_lockout_window_elapses():
    users = UserStore(max_failed_attempts=3, lockout_seconds=60)
    users.add_user("admin1", "admin-pass", "admin")

    for i in range(3):
        users.verify("admin1", "wrong", now=i)

    with pytest.raises(AccountLocked):
        users.verify("admin1", "admin-pass", now=10)

    # 60s past the lock (tripped at now=2, so expires at now=62).
    assert users.verify("admin1", "admin-pass", now=63) == "admin"


def test_successful_login_resets_failure_counter():
    users = UserStore(max_failed_attempts=3, lockout_seconds=60)
    users.add_user("admin1", "admin-pass", "admin")

    users.verify("admin1", "wrong", now=0)
    users.verify("admin1", "wrong", now=1)
    assert users.verify("admin1", "admin-pass", now=2) == "admin"  # resets counter

    # Two more failures shouldn't trip the (reset) 3-strike lock.
    users.verify("admin1", "wrong", now=3)
    assert users.verify("admin1", "wrong", now=4) is None  # still just a plain failure, not locked
    assert users.verify("admin1", "admin-pass", now=5) == "admin"


# -- API-level lockout + audit logging -----------------------------------

def test_repeated_failed_logins_lock_the_account_via_api(app_ctx):
    app, db_path = app_ctx
    client = app.test_client()

    for _ in range(3):
        resp = _login(client, "admin1", "wrong-pass")
        assert resp.status_code == 401

    locked_resp = _login(client, "admin1", "admin-pass")  # correct password, but locked now
    assert locked_resp.status_code == 423
    assert locked_resp.get_json()["error"] == "account_locked"


def test_locked_out_login_is_audited(app_ctx):
    app, db_path = app_ctx
    client = app.test_client()

    for _ in range(3):
        _login(client, "admin1", "wrong-pass")
    _login(client, "admin1", "admin-pass")

    events = [e["event_type"] for e in fetch_events(db_path)]
    assert events.count("auth_login_failed") == 3
    assert events.count("auth_account_locked") == 1


def test_successful_login_is_audited(app_ctx):
    app, db_path = app_ctx
    client = app.test_client()
    _login(client, "admin1", "admin-pass")

    events = [e for e in fetch_events(db_path) if e["event_type"] == "auth_login_success"]
    assert len(events) == 1


# -- TLS enforcement scaffold ---------------------------------------------

def test_tls_not_enforced_by_default(tmp_path):
    identity_path = tmp_path / "ctrl_id.json"
    system_path = tmp_path / "system_config.json"
    db_path = tmp_path / "event_log.db"
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(system_path, load_seed("system_config.seed.json"))
    init_db(db_path)
    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")

    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=NullNetworkApplier(), secret_key="test-secret",
    )
    app.testing = True
    resp = app.test_client().get("/api/status")
    assert resp.status_code == 200


def test_tls_required_rejects_plain_http_when_enabled(tmp_path):
    identity_path = tmp_path / "ctrl_id.json"
    system_path = tmp_path / "system_config.json"
    db_path = tmp_path / "event_log.db"
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(system_path, load_seed("system_config.seed.json"))
    init_db(db_path)
    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")

    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=NullNetworkApplier(), secret_key="test-secret",
        require_tls=True,
    )
    app.testing = True
    client = app.test_client()

    resp = _login(client, "admin1", "admin-pass")
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "tls_required"


def test_tls_required_allows_status_health_check_unconditionally(tmp_path):
    identity_path = tmp_path / "ctrl_id.json"
    system_path = tmp_path / "system_config.json"
    db_path = tmp_path / "event_log.db"
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(system_path, load_seed("system_config.seed.json"))
    init_db(db_path)
    users = UserStore()

    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=NullNetworkApplier(), secret_key="test-secret",
        require_tls=True,
    )
    app.testing = True
    resp = app.test_client().get("/api/status")
    assert resp.status_code == 200


def test_tls_required_accepts_trusted_proxy_forwarded_proto_header(tmp_path):
    identity_path = tmp_path / "ctrl_id.json"
    system_path = tmp_path / "system_config.json"
    db_path = tmp_path / "event_log.db"
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    config_store.atomic_write_json(system_path, load_seed("system_config.seed.json"))
    init_db(db_path)
    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")

    app = create_app(
        identity_path=identity_path, system_path=system_path, db_path=db_path,
        user_store=users, network_applier=NullNetworkApplier(), secret_key="test-secret",
        require_tls=True,
    )
    app.testing = True
    client = app.test_client()

    resp = client.post(
        "/api/login",
        json={"username": "admin1", "password": "admin-pass"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 200
