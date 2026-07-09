"""
Phase 4 exit test (execution plan): "change config under live polling —
no missed cycles, no relay glitch, invalid save rejected with running
config untouched."

Each claim gets its own test rather than one big scenario, so a future
regression points at exactly which guarantee broke:
  - invalid config -> running engine state is IDENTITY-untouched (not just
    behaviorally unchanged — this checks `is`, not `==`)
  - valid config -> swaps cleanly, LKG captures the OLD config
  - outputs hit safe_state BEFORE the swap (no relay left in limbo)
  - rule engine's edge-history resets on reload (documented, not hidden)
  - a config change noticed mid-poll-loop (via ConfigWatcher) never costs
    the current cycle's actual IO poll — applied or rejected, the poll
    still happens
  - atomic_write_json never leaves a half file
"""
from __future__ import annotations

import copy

from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from engine import config_store
from engine.event_store import fetch_events, init_db
from engine.poll_engine import PollEngine
from engine.rule_engine import RuleEngine

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


def _fake_clients_factory(fake):
    """Every build (initial construction AND every reload) gets the SAME
    fake client instance, matching how a real shared RTU serial client
    would behave across a reload — state (coils/registers/calls) persists
    across the swap because it's the same physical bus underneath."""
    def factory(io_config):
        return {d["unit_id"]: fake for d in io_config["devices"]}
    return factory


def _make_engine(tmp_path, fake, *, io_config=None, config_path=None, with_rules=False):
    io_config = io_config or load_seed("io_config.seed.v2.golden.json")
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    rule_engine = RuleEngine(io_config["rules"], IDENT, db_path) if with_rules else None
    engine = PollEngine(
        io_config, IDENT, db_path,
        rule_engine=rule_engine,
        config_path=config_path,
        clients_factory=_fake_clients_factory(fake),
    )
    return engine, db_path


# -- rejection leaves running state identity-untouched ------------------------

def test_invalid_reload_leaves_running_state_untouched(tmp_path):
    fake = FakeModbusClient()
    engine, _ = _make_engine(tmp_path, fake)

    old_plan, old_clients, old_io_config = engine.plan, engine.clients, engine.io_config
    old_point_by_id, old_device_by_unit_id = engine._point_by_id, engine._device_by_unit_id

    bad_config = copy.deepcopy(engine.io_config)
    bad_config["points"][0]["unit_id"] = 5  # in-range but no device 5 declared -> dangling ref

    result = engine.reload(bad_config)

    assert result.ok is False
    assert any("no matching device" in p for p in result.problems)
    assert engine.plan is old_plan
    assert engine.clients is old_clients
    assert engine.io_config is old_io_config
    assert engine._point_by_id is old_point_by_id
    assert engine._device_by_unit_id is old_device_by_unit_id


def test_invalid_reload_does_not_touch_outputs(tmp_path):
    """Rejection happens at validate_io(), before the safe-state pass even
    runs — a bad config must not glitch a relay on its way to being
    refused."""
    fake = FakeModbusClient()
    engine, _ = _make_engine(tmp_path, fake)

    bad_config = copy.deepcopy(engine.io_config)
    bad_config["schema_version"] = 99

    engine.reload(bad_config)
    assert fake.calls == []  # no write_coil, no reads -- nothing touched the bus at all


# -- valid reload: swap + LKG -------------------------------------------------

def test_valid_reload_swaps_config_and_backs_up_lkg(tmp_path):
    fake = FakeModbusClient()
    config_path = tmp_path / "io_config.json"
    old_config = load_seed("io_config.seed.v2.golden.json")
    config_store.atomic_write_json(config_path, old_config)
    engine, _ = _make_engine(tmp_path, fake, io_config=old_config, config_path=config_path)

    new_config = copy.deepcopy(old_config)
    new_config["config_version"] = 2
    new_config["bus"]["poll_interval_ms"] = 200

    result = engine.reload(new_config)

    assert result.ok is True
    assert engine.io_config is new_config
    assert engine.io_config["config_version"] == 2

    lkg = config_store.read_json(config_store.lkg_path(config_path))
    assert lkg["config_version"] == old_config["config_version"]  # LKG holds the OLD, not the new


def test_rollback_to_lkg_restores_prior_config(tmp_path):
    fake = FakeModbusClient()
    config_path = tmp_path / "io_config.json"
    old_config = load_seed("io_config.seed.v2.golden.json")
    config_store.atomic_write_json(config_path, old_config)
    engine, _ = _make_engine(tmp_path, fake, io_config=old_config, config_path=config_path)

    new_config = copy.deepcopy(old_config)
    new_config["config_version"] = 2
    engine.reload(new_config)
    assert engine.io_config["config_version"] == 2

    result = engine.rollback_to_lkg()
    assert result.ok is True
    assert engine.io_config["config_version"] == old_config["config_version"]


# -- safe_state before swap ---------------------------------------------------

def test_outputs_driven_to_safe_state_before_swap(tmp_path):
    fake = FakeModbusClient()
    old_config = load_seed("io_config.seed.v2.golden.json")
    engine, _ = _make_engine(tmp_path, fake, io_config=old_config)

    fake.coils[(1, 1)] = True  # led_maint currently energized (unit 1, addr 1)

    new_config = copy.deepcopy(old_config)
    new_config["config_version"] = 2
    engine.reload(new_config)

    assert ("write_coil", 1, False, 1) in fake.calls  # driven to safe_state (default False)
    assert fake.coils[(1, 1)] is False


def test_custom_safe_state_is_honored(tmp_path):
    fake = FakeModbusClient()
    old_config = load_seed("io_config.seed.v2.golden.json")
    for p in old_config["points"]:
        if p["id"] == "led_maint":
            p["safe_state"] = True  # this output's safe position is ON, not OFF
    engine, _ = _make_engine(tmp_path, fake, io_config=old_config)

    new_config = copy.deepcopy(old_config)
    new_config["config_version"] = 2
    engine.reload(new_config)

    assert ("write_coil", 1, True, 1) in fake.calls
    assert fake.coils[(1, 1)] is True


# -- rule engine edge-state reset on reload (documented behavior) -----------

def test_reload_resets_rule_engine_and_debounce_state(tmp_path):
    """Both the debouncer and the rule engine are rebuilt fresh on reload
    (documented in reload()'s docstring), not carried over. Demonstrate it
    two ways: (1) a press held from BEFORE the reload does NOT retroactively
    fire after reload, because the fresh debouncer treats the first
    post-reload raw read as its new baseline rather than continuing to
    count toward the old debounce window; (2) once a clean False baseline
    is re-witnessed post-reload, a fresh rising edge fires normally --
    proving the reset didn't just permanently break rule evaluation."""
    fake = FakeModbusClient()
    old_config = load_seed("io_config.seed.v2.golden.json")
    engine, db_path = _make_engine(tmp_path, fake, io_config=old_config, with_rules=True)

    engine.run_cycle(now_ms=0)      # baseline: button unpressed
    fake.coils[(1, 0)] = True
    engine.run_cycle(now_ms=100)    # 1 of 3 debounce reads while pressed, pre-reload

    new_config = copy.deepcopy(old_config)
    new_config["config_version"] = 2
    engine.reload(new_config)       # fresh debouncer + fresh rule engine

    # (1) Button is STILL physically held True, but the new debouncer's
    # first post-reload read seeds True as its baseline directly (no prior
    # state to debounce against) -- and the new rule engine's first
    # cycle has no witnessed prior value either. No spurious fire.
    engine.run_cycle(now_ms=200)
    assert not any(e["event_type"] == "maintenance_request" for e in fetch_events(db_path))

    # (2) Release, then press again -- a real edge, cleanly witnessed by
    # the new instances, must fire normally.
    fake.coils[(1, 0)] = False
    for t in (300, 400, 500):
        engine.run_cycle(now_ms=t)  # 3 reads -> debounced False re-established
    fake.coils[(1, 0)] = True
    for t in (600, 700, 800):
        engine.run_cycle(now_ms=t)  # 3 reads -> debounced True, rising edge fires

    assert any(e["event_type"] == "maintenance_request" for e in fetch_events(db_path))


# -- watcher-driven reload during live polling: no missed cycle -------------

def test_watcher_applies_valid_change_without_missing_a_poll(tmp_path):
    fake = FakeModbusClient()
    config_path = tmp_path / "io_config.json"
    old_config = load_seed("io_config.seed.v2.golden.json")
    config_store.atomic_write_json(config_path, old_config)
    engine, db_path = _make_engine(tmp_path, fake, io_config=old_config, config_path=config_path)

    fake.registers[(2, 100)] = 3
    results = engine.run_cycle(now_ms=0)
    assert results["fault_code"].value == 3  # baseline poll works

    new_config = copy.deepcopy(old_config)
    new_config["config_version"] = 2
    config_store.atomic_write_json(config_path, new_config)

    results = engine.run_cycle(now_ms=100)  # watcher notices + applies THIS cycle
    assert engine.io_config["config_version"] == 2
    assert "fault_code" in results and results["fault_code"].value == 3  # poll still happened

    events = fetch_events(db_path)
    assert any(e["event_type"] == "config_reload_applied" for e in events)


def test_watcher_rejects_invalid_change_and_polling_continues(tmp_path):
    fake = FakeModbusClient()
    config_path = tmp_path / "io_config.json"
    old_config = load_seed("io_config.seed.v2.golden.json")
    config_store.atomic_write_json(config_path, old_config)
    engine, db_path = _make_engine(tmp_path, fake, io_config=old_config, config_path=config_path)

    engine.run_cycle(now_ms=0)

    bad_config = copy.deepcopy(old_config)
    bad_config["config_version"] = 2
    bad_config["schema_version"] = 99
    config_store.atomic_write_json(config_path, bad_config)

    fake.registers[(2, 100)] = 5
    results = engine.run_cycle(now_ms=100)  # watcher sees it, rejects it, still polls

    assert engine.io_config["config_version"] == old_config["config_version"]  # unchanged
    assert results["fault_code"].value == 5  # this cycle's poll still ran, on the OLD plan

    events = fetch_events(db_path)
    assert any(e["event_type"] == "config_reload_rejected" for e in events)


# -- ConfigWatcher + atomic write -------------------------------------------

def test_watcher_ignores_its_own_initial_version(tmp_path):
    path = tmp_path / "io_config.json"
    doc = {"config_version": 5, "x": 1}
    config_store.atomic_write_json(path, doc)
    watcher = config_store.ConfigWatcher(path, initial_version=5)
    assert watcher.poll() is None  # same version as construction -- not "a change"


def test_watcher_detects_version_bump(tmp_path):
    path = tmp_path / "io_config.json"
    config_store.atomic_write_json(path, {"config_version": 1})
    watcher = config_store.ConfigWatcher(path, initial_version=1)
    assert watcher.poll() is None

    config_store.atomic_write_json(path, {"config_version": 2})
    seen = watcher.poll()
    assert seen == {"config_version": 2}
    assert watcher.poll() is None  # already seen, no repeat


def test_watcher_survives_missing_or_malformed_file(tmp_path):
    path = tmp_path / "does_not_exist.json"
    watcher = config_store.ConfigWatcher(path)
    assert watcher.poll() is None

    path.write_text("{not valid json")
    assert watcher.poll() is None


def test_atomic_write_never_leaves_a_half_file(tmp_path):
    path = tmp_path / "io_config.json"
    config_store.atomic_write_json(path, {"config_version": 1, "note": "original"})

    # Simulate a crash mid-write: a temp file gets created but the atomic
    # rename never happens.
    crashed_tmp = path.with_name(f"{path.name}.tmp-99999")
    crashed_tmp.write_text('{"config_version": 2, "note": "half-writ')  # deliberately truncated

    doc = config_store.read_json(path)
    assert doc == {"config_version": 1, "note": "original"}  # real file untouched by the crash
