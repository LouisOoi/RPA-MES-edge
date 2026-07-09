import pytest
from conftest import load_seed

from migrations import LATEST_SCHEMA_VERSION, MigrationError, migrate_to_latest
from validators import validate_io


def _strip_volatile(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("updated_at", None)
    return doc


def test_v1_to_v2_matches_golden_file():
    v1 = load_seed("io_config.seed.v1.json")
    golden = load_seed("io_config.seed.v2.golden.json")

    migrated = migrate_to_latest(v1, updated_by="migration_test")

    assert _strip_volatile(migrated) == _strip_volatile(golden)
    assert migrated["schema_version"] == LATEST_SCHEMA_VERSION


def test_migrated_config_passes_v2_validation():
    v1 = load_seed("io_config.seed.v1.json")
    migrated = migrate_to_latest(v1, updated_by="migration_test")
    validate_io(migrated)  # must not raise


def test_migrate_is_a_noop_at_latest_version():
    golden = load_seed("io_config.seed.v2.golden.json")
    result = migrate_to_latest(golden, updated_by="migration_test")
    assert result is golden


def test_migrate_refuses_future_schema_version():
    doc = load_seed("io_config.seed.v2.golden.json")
    doc = dict(doc, schema_version=99)
    with pytest.raises(MigrationError):
        migrate_to_latest(doc, updated_by="migration_test")


def test_migration_refuses_unknown_topology():
    v1 = load_seed("io_config.seed.v1.json")
    # Add a second digital_out on the same unit_id as the button -> ambiguous
    # sibling relationship the narrow migration explicitly refuses to guess at.
    v1["points"].append({"id": "led_extra", "unit_id": 1, "kind": "digital_out", "address": 2})
    with pytest.raises(MigrationError):
        migrate_to_latest(v1, updated_by="migration_test")


def test_exit_criterion_seed_reproduces_hardcoded_behavior():
    """Phase 0 exit test (execution plan): the seed config must encode the
    exact constants from factory_iot_reference.md's modbus_poll.py, so that
    a Phase-2 engine driven by this config produces identical behavior to
    today's hardcoded script. This test pins those constants so a future
    edit to the seed can't silently drift from the reference doc.
    """
    v1 = load_seed("io_config.seed.v1.json")
    assert v1["bus"]["port"] == "/dev/ttyS0"
    assert v1["bus"]["baudrate"] == 19200
    assert v1["bus"]["poll_interval_ms"] == 100  # time.sleep(0.1) in modbus_poll.py

    by_id = {p["id"]: p for p in v1["points"]}
    assert by_id["btn_maint"]["unit_id"] == 1        # IO_MODULE_ADDR
    assert by_id["btn_maint"]["address"] == 0        # BUTTON_COIL
    assert by_id["btn_maint"]["debounce_reads"] == 3  # DEBOUNCE_COUNT
    assert by_id["led_maint"]["unit_id"] == 1        # IO_MODULE_ADDR
    assert by_id["led_maint"]["address"] == 1        # LED_COIL
    assert by_id["fault_code"]["unit_id"] == 2       # PLC_ADDR
    assert by_id["fault_code"]["address"] == 100     # FAULT_REGISTER

    migrated = migrate_to_latest(v1, updated_by="migration_test")
    # 3 reads x 100ms poll interval = 300ms debounce, matching the reference
    # doc's RS485 table ("300 ms SW debounce for button coil").
    assert next(p for p in migrated["points"] if p["id"] == "btn_maint")["debounce_ms"] == 300
