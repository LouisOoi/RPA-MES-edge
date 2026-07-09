import copy

import pytest
from conftest import load_seed

from validators import ConfigValidationError, validate_identity, validate_io, validate_system


def test_identity_seed_valid():
    validate_identity(load_seed("ctrl_id.seed.json"))


def test_identity_rejects_bad_boot_id():
    doc = load_seed("ctrl_id.seed.json")
    doc["boot_id"] = "not-a-uuid"
    with pytest.raises(ConfigValidationError):
        validate_identity(doc)


def test_identity_rejects_missing_field():
    doc = load_seed("ctrl_id.seed.json")
    del doc["station_id"]
    with pytest.raises(ConfigValidationError):
        validate_identity(doc)


def test_system_seed_valid():
    validate_system(load_seed("system_config.seed.json"))


def test_system_tls_requires_certs():
    doc = load_seed("system_config.seed.json")
    del doc["mqtt"]["ca_cert"]
    with pytest.raises(ConfigValidationError):
        validate_system(doc)


def test_system_static_requires_ip():
    doc = load_seed("system_config.seed.json")
    del doc["network"]["ip"]
    with pytest.raises(ConfigValidationError):
        validate_system(doc)


def test_io_v1_seed_valid():
    validate_io(load_seed("io_config.seed.v1.json"))


def test_io_v2_golden_valid():
    validate_io(load_seed("io_config.seed.v2.golden.json"))


def test_io_v2_rejects_analog_in_by_default():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["points"].append({
        "id": "temp_oven", "name": "Oven Temp", "unit_id": 2, "kind": "analog_in",
        "modbus": {"fn": "read_input_registers", "address": 0, "count": 1},
        "scaling": {"raw_min": 0, "raw_max": 4095, "eng_min": 0, "eng_max": 200},
        "unit": "C", "invert": False,
    })
    with pytest.raises(ConfigValidationError) as exc:
        validate_io(doc)
    assert any("analog_in" in p for p in exc.value.problems)


def test_io_v2_allows_analog_in_when_policy_cap_lifted():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["points"].append({
        "id": "temp_oven", "name": "Oven Temp", "unit_id": 2, "kind": "analog_in",
        "modbus": {"fn": "read_input_registers", "address": 0, "count": 1},
        "scaling": {"raw_min": 0, "raw_max": 4095, "eng_min": 0, "eng_max": 200},
        "unit": "C", "invert": False,
    })
    validate_io(doc, enforce_v1_policy_caps=False)


def test_io_v2_rejects_multi_condition_rule_by_default():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["rules"][0]["when"].append({"point": "fault_code", "op": ">", "value": 0})
    with pytest.raises(ConfigValidationError) as exc:
        validate_io(doc)
    assert any("rule-engine v1" in p for p in exc.value.problems)


def test_io_v2_rejects_dangling_point_ref():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["rules"][0]["when"][0]["point"] = "does_not_exist"
    with pytest.raises(ConfigValidationError) as exc:
        validate_io(doc)
    assert any("unknown point" in p for p in exc.value.problems)


def test_io_v2_rejects_output_contention():
    doc = load_seed("io_config.seed.v2.golden.json")
    extra_rule = copy.deepcopy(doc["rules"][0])
    extra_rule["id"] = "rule_contending"
    extra_rule["when"] = [{"point": "fault_code", "op": ">", "value": 0}]
    doc["rules"].append(extra_rule)  # also pulses led_maint -> contention with rule_btn_maint
    with pytest.raises(ConfigValidationError) as exc:
        validate_io(doc)
    assert any("output contention" in p for p in exc.value.problems)


def test_io_v2_allows_same_rule_writing_same_point_in_then_and_else():
    """then/else within ONE rule are mutually exclusive at runtime -- only
    one branch ever fires per cycle -- so this is not contention with
    itself, even though the same point is written from both branches."""
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["rules"][0]["then"] = [{"action": "set", "point": "led_maint", "value": True}]
    doc["rules"][0]["else"] = [{"action": "set", "point": "led_maint", "value": False}]
    validate_io(doc)  # must not raise


def test_io_v2_rejects_point_with_unknown_device():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["points"][0]["unit_id"] = 99
    with pytest.raises(ConfigValidationError) as exc:
        validate_io(doc)
    assert any("no matching device" in p for p in exc.value.problems)


def test_io_rejects_unsupported_schema_version():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["schema_version"] = 99
    with pytest.raises(ConfigValidationError):
        validate_io(doc)
