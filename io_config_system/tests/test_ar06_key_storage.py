"""
AR-06 — secure-element/TPM key storage scaffold. See
IO_Config_Execution_Plan.md's AR-06 row: the MQTT client TLS private key
must eventually be provisioned into hardware, never sit in an extractable
file. No such hardware exists in this environment; these tests cover the
KeyStore interface contract and the honest warning surfaced today.
"""
from __future__ import annotations

import pytest

from engine.key_storage import FileKeyStore, KeyStorageUnavailable, Pkcs11KeyStore
from engine.system_store import ApplyResult, flag_extractable_client_key, save_system_config


def test_file_key_store_is_honestly_extractable():
    store = FileKeyStore("/etc/certs/client.key")
    assert store.is_extractable() is True
    assert store.get_client_key_reference() == "/etc/certs/client.key"


def test_pkcs11_key_store_fails_loudly_without_a_real_module(tmp_path):
    store = Pkcs11KeyStore(pkcs11_uri="pkcs11:token=edge01;object=mqtt-key", module_path=tmp_path / "no-such-module.so")
    with pytest.raises(KeyStorageUnavailable):
        store.get_client_key_reference()


def test_pkcs11_key_store_works_when_module_and_uri_are_present(tmp_path):
    module = tmp_path / "opensc-pkcs11.so"
    module.write_bytes(b"not a real module, just needs to exist")
    store = Pkcs11KeyStore(pkcs11_uri="pkcs11:token=edge01;object=mqtt-key", module_path=module)
    assert store.get_client_key_reference() == "pkcs11:token=edge01;object=mqtt-key"
    assert store.is_extractable() is False


def test_flag_extractable_client_key_flags_a_configured_path():
    assert flag_extractable_client_key({"client_key": "/etc/certs/client.key"}) == ["/etc/certs/client.key"]


def test_flag_extractable_client_key_is_clean_when_unset():
    assert flag_extractable_client_key({}) == []


class _AlwaysOkApplier:
    def apply(self, system_config):
        return ApplyResult(ok=True, message="ok")


def test_save_system_config_warns_about_extractable_client_key(tmp_path):
    path = tmp_path / "system_config.json"
    config = {
        "network": {"mode": "static", "ip": "192.168.10.9", "mask": "255.255.255.0",
                    "gateway": "192.168.10.1", "dns": ["192.168.10.1"]},
        "mqtt": {"broker_host": "mqtt.factory.local", "port": 8883, "tls": True,
                 "ca_cert": "/etc/certs/ca.crt", "client_cert": "/etc/certs/client.crt",
                 "client_key": "/etc/certs/client.key"},
        "time": {"ntp": ["ntp.local"], "timezone": "Asia/Kuala_Lumpur", "rtc_present": True},
    }
    result = save_system_config(path, config, _AlwaysOkApplier())
    assert result.ok
    assert any("AR-06" in w for w in result.warnings)
