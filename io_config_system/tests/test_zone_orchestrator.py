"""
Multi-zone orchestrator (Deployment Variant B). See
IO_Config_Execution_Plan.md's orchestrator item: "runs N independent
PollEngine/RuleEngine/config_store instances... each supervised on its
own thread so one zone's fault or wireless dropout doesn't affect the
others." These tests cover the pure loop logic directly (no threads,
deterministic) and the ZoneOrchestrator's real-thread behavior: fault
isolation between zones, crash logging, and restart-with-backoff.
"""
from __future__ import annotations

import threading
import time

import pytest

from engine.event_store import fetch_events, init_db
from engine.zone_orchestrator import ZoneOrchestrator, _zone_loop

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z01", "station_id": "ST01",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


class _FakeEngine:
    """A minimal stand-in exposing exactly what _zone_loop/ZoneOrchestrator
    touch: io_config['bus']['poll_interval_ms'], run_cycle(), db_path,
    ident. Using this instead of a real PollEngine keeps these tests
    about the ORCHESTRATOR's behavior, not re-testing PollEngine."""

    def __init__(self, db_path, *, poll_interval_ms=5, fail_from_call=None, fail_forever=True):
        self.io_config = {"bus": {"poll_interval_ms": poll_interval_ms}}
        self.db_path = db_path
        self.ident = IDENT
        self.call_count = 0
        self.fail_from_call = fail_from_call
        self.fail_forever = fail_forever
        self._lock = threading.Lock()

    def run_cycle(self):
        with self._lock:
            self.call_count += 1
            count = self.call_count
        if self.fail_from_call is not None and count >= self.fail_from_call:
            if self.fail_forever or count == self.fail_from_call:
                raise RuntimeError(f"simulated crash on call {count}")


def _wait_until(predicate, *, timeout_s=2.0, interval_s=0.01):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


# -- _zone_loop: pure, no threads --------------------------------------------

def test_zone_loop_runs_exactly_max_cycles_then_stops(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = _FakeEngine(db_path, poll_interval_ms=1)
    stop_event = threading.Event()
    errors = []
    cycles = _zone_loop("z1", engine, stop_event, on_error=lambda zid, exc: errors.append(exc), max_cycles=5)
    assert cycles == 5
    assert engine.call_count == 5
    assert errors == []


def test_zone_loop_stops_immediately_if_stop_event_already_set(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = _FakeEngine(db_path, poll_interval_ms=1)
    stop_event = threading.Event()
    stop_event.set()
    cycles = _zone_loop("z1", engine, stop_event, on_error=lambda zid, exc: None)
    assert cycles == 0
    assert engine.call_count == 0


def test_zone_loop_reports_and_reraises_on_crash(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = _FakeEngine(db_path, poll_interval_ms=1, fail_from_call=2)
    stop_event = threading.Event()
    seen = []
    with pytest.raises(RuntimeError):
        _zone_loop("z1", engine, stop_event, on_error=lambda zid, exc: seen.append((zid, str(exc))))
    assert len(seen) == 1
    assert seen[0][0] == "z1"
    assert engine.call_count == 2  # cycle 1 ok, cycle 2 crashed


# -- ZoneOrchestrator: real threads ------------------------------------------

def test_add_zone_rejects_duplicate_zone_id(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    orchestrator = ZoneOrchestrator()
    orchestrator.add_zone("weld_cell", _FakeEngine(db_path))
    with pytest.raises(ValueError):
        orchestrator.add_zone("weld_cell", _FakeEngine(db_path))


def test_unregistered_zone_lookup_raises_keyerror(tmp_path):
    orchestrator = ZoneOrchestrator()
    with pytest.raises(KeyError):
        orchestrator.get_engine("nonexistent")


def test_healthy_zone_runs_continuously_once_started(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = _FakeEngine(db_path, poll_interval_ms=5)
    orchestrator = ZoneOrchestrator()
    orchestrator.add_zone("weld_cell", engine)
    orchestrator.start_zone("weld_cell")
    try:
        assert _wait_until(lambda: engine.call_count >= 5)
        assert orchestrator.is_running("weld_cell") is True
        assert orchestrator.crash_count("weld_cell") == 0
    finally:
        orchestrator.stop_zone("weld_cell", timeout_s=2.0)
    assert orchestrator.is_running("weld_cell") is False


def test_one_zones_crash_does_not_stop_or_slow_a_healthy_zone(tmp_path):
    """The actual property the plan requires: "one zone's fault... doesn't
    affect the others." Zone A crashes on every cycle; Zone B never
    fails. Zone B must keep accumulating calls at its normal rate
    regardless of what's happening to Zone A."""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    init_db(db_a)
    init_db(db_b)
    engine_a = _FakeEngine(db_a, poll_interval_ms=5, fail_from_call=1, fail_forever=True)
    engine_b = _FakeEngine(db_b, poll_interval_ms=5)

    orchestrator = ZoneOrchestrator(restart_backoff_s=0.02)
    orchestrator.add_zone("flaky", engine_a)
    orchestrator.add_zone("healthy", engine_b)
    orchestrator.start_all()
    try:
        assert _wait_until(lambda: orchestrator.crash_count("flaky") >= 3)
        assert _wait_until(lambda: engine_b.call_count >= 5)
        # Zone B's healthy engine never saw a single failure attributed to it.
        assert orchestrator.crash_count("healthy") == 0
        assert orchestrator.is_running("healthy") is True
        # Zone A keeps getting restarted (still "running" despite crashing).
        assert orchestrator.is_running("flaky") is True
    finally:
        orchestrator.stop_all(timeout_s=2.0)
    assert orchestrator.is_running("flaky") is False
    assert orchestrator.is_running("healthy") is False


def test_crash_is_logged_with_zone_id_and_error(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = _FakeEngine(db_path, poll_interval_ms=5, fail_from_call=1, fail_forever=True)
    orchestrator = ZoneOrchestrator(restart_backoff_s=0.02)
    orchestrator.add_zone("weld_cell", engine)
    orchestrator.start_zone("weld_cell")
    try:
        assert _wait_until(lambda: orchestrator.crash_count("weld_cell") >= 1)
    finally:
        orchestrator.stop_zone("weld_cell", timeout_s=2.0)

    events = [e for e in fetch_events(db_path) if e["event_type"] == "zone_thread_crashed"]
    assert len(events) >= 1
    assert orchestrator.last_error("weld_cell") is not None


def test_transient_crash_recovers_and_keeps_running(tmp_path):
    """Zone crashes exactly once then behaves — the restart must not
    permanently wedge the zone."""
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = _FakeEngine(db_path, poll_interval_ms=5, fail_from_call=2, fail_forever=False)
    orchestrator = ZoneOrchestrator(restart_backoff_s=0.02)
    orchestrator.add_zone("weld_cell", engine)
    orchestrator.start_zone("weld_cell")
    try:
        assert _wait_until(lambda: orchestrator.crash_count("weld_cell") == 1)
        assert _wait_until(lambda: engine.call_count >= 6)
        assert orchestrator.is_running("weld_cell") is True
    finally:
        orchestrator.stop_zone("weld_cell", timeout_s=2.0)


def test_max_restarts_gives_up_after_the_budget_is_exhausted(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = _FakeEngine(db_path, poll_interval_ms=5, fail_from_call=1, fail_forever=True)
    orchestrator = ZoneOrchestrator(restart_backoff_s=0.01, max_restarts=2)
    orchestrator.add_zone("weld_cell", engine)
    orchestrator.start_zone("weld_cell")

    assert _wait_until(lambda: orchestrator.is_running("weld_cell") is False, timeout_s=3.0)
    assert orchestrator.crash_count("weld_cell") >= 3  # exceeded the 2-restart budget
    orchestrator.stop_zone("weld_cell", timeout_s=2.0)  # idempotent even though it already stopped


def test_remove_zone_stops_it_and_forgets_it(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = _FakeEngine(db_path, poll_interval_ms=5)
    orchestrator = ZoneOrchestrator()
    orchestrator.add_zone("weld_cell", engine)
    orchestrator.start_zone("weld_cell")
    assert _wait_until(lambda: engine.call_count >= 2)

    orchestrator.remove_zone("weld_cell", timeout_s=2.0)
    assert "weld_cell" not in orchestrator.zone_ids()
    with pytest.raises(KeyError):
        orchestrator.get_engine("weld_cell")
