import socket
import threading

import pytest
from conftest import load_seed

from engine import config_store
from engine.system_store import (
    AlwaysFailNetworkApplier,
    NullNetworkApplier,
    SystemUpdateError,
    load_system_config,
    save_system_config,
    check_mqtt_connection,
)


def _setup(tmp_path):
    path = tmp_path / "system_config.json"
    config_store.atomic_write_json(path, load_seed("system_config.seed.json"))
    return path


def test_load_returns_seed(tmp_path):
    path = _setup(tmp_path)
    assert load_system_config(path)["mqtt"]["broker_host"] == "mqtt.factory.local"


def test_save_valid_config_succeeds_and_applier_records_it(tmp_path):
    path = _setup(tmp_path)
    new_config = load_seed("system_config.seed.json")
    new_config["network"]["ip"] = "192.168.10.50"
    applier = NullNetworkApplier()

    result = save_system_config(path, new_config, applier)

    assert result.ok is True
    assert applier.applied == [new_config]
    assert config_store.read_json(path)["network"]["ip"] == "192.168.10.50"


def test_save_invalid_config_is_rejected_and_not_written(tmp_path):
    path = _setup(tmp_path)
    original = config_store.read_json(path)
    bad_config = load_seed("system_config.seed.json")
    del bad_config["mqtt"]["ca_cert"]  # tls:true requires certs

    with pytest.raises(SystemUpdateError):
        save_system_config(path, bad_config, NullNetworkApplier())

    assert config_store.read_json(path) == original


def test_save_not_written_when_applier_fails(tmp_path):
    """If the privileged helper couldn't actually apply the network
    change, the file must not be updated to claim otherwise."""
    path = _setup(tmp_path)
    original = config_store.read_json(path)
    new_config = load_seed("system_config.seed.json")
    new_config["network"]["ip"] = "192.168.10.99"

    result = save_system_config(path, new_config, AlwaysFailNetworkApplier("dhcpcd restart failed"))

    assert result.ok is False
    assert "dhcpcd restart failed" in result.message
    assert config_store.read_json(path) == original


def test_check_mqtt_connection_succeeds_against_reachable_host(tmp_path):
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
            except (socket.timeout, OSError):
                continue

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        result = check_mqtt_connection(host, port, timeout_s=1.0)
        assert result.ok is True
    finally:
        stop.set()
        server.close()
        t.join(timeout=1)


def test_check_mqtt_connection_fails_against_closed_port():
    # Port 1 is privileged/almost certainly not listening in this sandbox.
    result = check_mqtt_connection("127.0.0.1", 1, timeout_s=0.5)
    assert result.ok is False
    assert "failed" in result.message
