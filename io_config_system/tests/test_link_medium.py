"""
Multi-zone `link.medium` — schema field (per-zone, optional, purely
descriptive to the engine) plus the recommended commissioning defaults
helper. See engine/link_medium.py's module docstring.
"""
from __future__ import annotations

import pytest
from conftest import load_seed

from engine.link_medium import recommended_comms_defaults
from validators import ConfigValidationError, validate_io


def test_io_config_without_link_field_is_still_valid():
    """Single-zone (Variant A) configs never set `link` at all."""
    doc = load_seed("io_config.seed.v2.golden.json")
    assert "link" not in doc
    validate_io(doc)  # must not raise


def test_io_config_accepts_wired_link_medium():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["link"] = {"medium": "wired"}
    validate_io(doc)


def test_io_config_accepts_wireless_link_medium():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["link"] = {"medium": "wireless"}
    validate_io(doc)


def test_io_config_rejects_unknown_link_medium():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["link"] = {"medium": "satellite"}
    with pytest.raises(ConfigValidationError):
        validate_io(doc)


def test_link_medium_requires_the_medium_key():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc["link"] = {}
    with pytest.raises(ConfigValidationError):
        validate_io(doc)


def test_recommended_defaults_differ_between_wired_and_wireless():
    wired = recommended_comms_defaults("wired")
    wireless = recommended_comms_defaults("wireless")
    assert wired["timeout_ms"] < wireless["timeout_ms"]
    assert wired["poll_interval_ms"] < wireless["poll_interval_ms"]


def test_recommended_defaults_returns_a_copy_not_the_shared_dict():
    a = recommended_comms_defaults("wired")
    a["timeout_ms"] = 999999
    b = recommended_comms_defaults("wired")
    assert b["timeout_ms"] != 999999


def test_recommended_defaults_rejects_unknown_medium():
    with pytest.raises(ValueError):
        recommended_comms_defaults("satellite")
