"""
Regression tests for the Architecture Review remediation items that landed
as real schema/validator changes: AR-01 (output_class allow-list) and AR-03
(single-owner-per-output). See IO_Config_Execution_Plan.md's "Architecture
Review remediation (v1)" section and Architecture_Review.md for the full
reasoning — these tests only pin down the resulting behavior.
"""
from __future__ import annotations

import copy

import pytest
from conftest import load_seed

from validators import ConfigValidationError, validate_io


def _base_config() -> dict:
    return copy.deepcopy(load_seed("io_config.seed.v2.golden.json"))


# ---- AR-01: output_class allow-list -----------------------------------

def test_output_class_required_for_edge_owned_output():
    doc = _base_config()
    led = next(p for p in doc["points"] if p["id"] == "led_maint")
    del led["output_class"]  # owner defaults to "edge" per schema

    with pytest.raises(ConfigValidationError) as exc:
        validate_io(doc)
    assert any("output_class is missing" in p for p in exc.value.problems)


def test_output_class_rejects_disallowed_value():
    doc = _base_config()
    led = next(p for p in doc["points"] if p["id"] == "led_maint")
    led["output_class"] = "safety_interlock"  # not in the schema enum — no escape hatch

    with pytest.raises(ConfigValidationError):
        validate_io(doc)


def test_output_class_must_be_null_for_plc_owned_output():
    doc = _base_config()
    led = next(p for p in doc["points"] if p["id"] == "led_maint")
    led["owner"] = "plc"
    led["output_class"] = "indicator"  # left over from before the ownership flip

    with pytest.raises(ConfigValidationError) as exc:
        validate_io(doc)
    assert any("must not carry an output_class" in p for p in exc.value.problems)


def test_indicator_and_non_safety_actuation_both_pass():
    for cls in ("indicator", "non_safety_actuation"):
        doc = _base_config()
        led = next(p for p in doc["points"] if p["id"] == "led_maint")
        led["output_class"] = cls
        validate_io(doc)  # must not raise


# ---- AR-03: single owner per output -----------------------------------

def test_rule_cannot_write_a_plc_owned_point():
    doc = _base_config()
    led = next(p for p in doc["points"] if p["id"] == "led_maint")
    led["owner"] = "plc"
    del led["output_class"]  # plc-owned: must be absent/null, not "indicator"

    # rule_btn_maint (from the golden seed) still targets led_maint in "then"
    with pytest.raises(ConfigValidationError) as exc:
        validate_io(doc)
    assert any("owned by the machine PLC" in p for p in exc.value.problems)


def test_rule_may_read_but_not_write_a_plc_owned_point():
    doc = _base_config()
    led = next(p for p in doc["points"] if p["id"] == "led_maint")
    led["owner"] = "plc"
    del led["output_class"]

    # Retarget the rule to only READ led_maint (as a condition), not write it.
    doc["rules"] = [{
        "id": "rule_led_observed",
        "enabled": True,
        "match": "all",
        "when": [{"point": "led_maint", "op": "rising"}],
        "then": [{"action": "log_event", "event_type": "led_observed_on"}],
        "else": [],
    }]
    validate_io(doc)  # reading a plc-owned point is fine; must not raise


def test_edge_owned_output_can_still_be_written_by_a_rule():
    doc = _base_config()
    # led_maint stays owner="edge" (the golden default) — rule_btn_maint
    # writing it via pulse/set must keep working; this is the regression
    # guard that AR-03 didn't accidentally block edge-owned writes too.
    validate_io(doc)
