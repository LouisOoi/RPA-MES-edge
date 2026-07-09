"""
Phase 3 exit test (execution plan): "unit tests per operator + a
'button->LED' rule reproducing the reference's fixed logic purely from
config."

Two layers of test here:
  1. Pure RuleEngine tests against a stub poll engine — one per whitelisted
     operator, plus match/else/disabled/stale/edge semantics. No Modbus
     involved; these are about the interpreter, not the bus.
  2. The capstone: real PollEngine + RuleEngine + FakeModbusClient driven
     by the migrated golden config (rule_btn_maint, rule_fault_code_fault —
     synthesized by Phase 0's migration from the exact reference topology).
     This is what finally closes the caveat Phase 2 flagged: with rules
     wired in, button press -> LED coil + maintenance_request event, and
     fault register -> machine_fault event, purely from io_config.json.
"""
from __future__ import annotations

import pytest
from conftest import load_seed
from fake_modbus_client import FakeModbusClient

from engine.event_store import fetch_events, init_db
from engine.point_io import ReadResult
from engine.poll_engine import PollEngine
from engine.rule_engine import RuleEngine

IDENT = {
    "plant_id": "PLT01", "line_id": "L03", "zone_id": "Z02", "station_id": "ST07",
    "boot_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}


class StubPollEngine:
    """Records writes without touching any Modbus client — used for the
    pure operator/action tests where the point of the test is the rule
    interpreter, not IO."""

    def __init__(self):
        self.writes: list[tuple[str, bool]] = []

    def write_point(self, point_id: str, value: bool) -> ReadResult:
        self.writes.append((point_id, value))
        return ReadResult(value=value, stale=False)


def _results(**point_values) -> dict[str, ReadResult]:
    return {pid: ReadResult(value=v, stale=False) for pid, v in point_values.items()}


def _stale(point_id: str) -> dict[str, ReadResult]:
    return {point_id: ReadResult(value=None, stale=True)}


def _rule(when, then=None, else_=None, match="all", enabled=True, rule_id="r1"):
    return {
        "id": rule_id, "enabled": enabled, "match": match,
        "when": when, "then": then or [], "else": else_ or [],
    }


# -- per-operator tests -------------------------------------------------------

@pytest.mark.parametrize("op,value,cond_extra,expect", [
    (">",  11, {"value": 10}, True),
    (">",   9, {"value": 10}, False),
    ("<",   9, {"value": 10}, True),
    ("<",  11, {"value": 10}, False),
    (">=", 10, {"value": 10}, True),
    (">=",  9, {"value": 10}, False),
    ("<=", 10, {"value": 10}, True),
    ("<=", 11, {"value": 10}, False),
    ("==", 10, {"value": 10}, True),
    ("==", 11, {"value": 10}, False),
    ("between", 5, {"low": 1, "high": 10}, True),
    ("between", 11, {"low": 1, "high": 10}, False),
])
def test_comparison_operator(op, value, cond_extra, expect, tmp_path):
    db_path = tmp_path / f"e_{op.replace('=', 'eq').replace('<', 'lt').replace('>', 'gt')}_{value}.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        when=[{"point": "reg", "op": op, **cond_extra}],
        then=[{"action": "log_event", "event_type": "fired"}],
    )], IDENT, db_path)
    stub = StubPollEngine()
    fired = engine.evaluate_cycle(stub, _results(reg=value), now_ms=0)
    assert (fired == ["r1"]) == expect


def test_rising_requires_a_witnessed_prior_false(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        when=[{"point": "btn", "op": "rising"}],
        then=[{"action": "log_event", "event_type": "fired"}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    # First cycle ever sees btn=True — no prior value witnessed, must NOT fire.
    fired = engine.evaluate_cycle(stub, _results(btn=True), now_ms=0)
    assert fired == []

    # Cycle 2: False (establishes a witnessed prior of False)
    engine.evaluate_cycle(stub, _results(btn=False), now_ms=100)
    # Cycle 3: True again — now it's a real witnessed False->True transition
    fired = engine.evaluate_cycle(stub, _results(btn=True), now_ms=200)
    assert fired == ["r1"]
    # Cycle 4: stays True — not a new edge, must not re-fire
    fired = engine.evaluate_cycle(stub, _results(btn=True), now_ms=300)
    assert fired == []


def test_falling_requires_a_witnessed_prior_true(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        when=[{"point": "btn", "op": "falling"}],
        then=[{"action": "log_event", "event_type": "fired"}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    engine.evaluate_cycle(stub, _results(btn=True), now_ms=0)
    fired = engine.evaluate_cycle(stub, _results(btn=False), now_ms=100)
    assert fired == ["r1"]


# -- match / else / disabled / stale semantics --------------------------------

def test_match_all_requires_every_condition(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        match="all",
        when=[{"point": "a", "op": ">", "value": 0}, {"point": "b", "op": ">", "value": 0}],
        then=[{"action": "log_event", "event_type": "fired"}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    assert engine.evaluate_cycle(stub, _results(a=1, b=0), now_ms=0) == []
    assert engine.evaluate_cycle(stub, _results(a=1, b=1), now_ms=100) == ["r1"]


def test_match_any_requires_one_condition(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        match="any",
        when=[{"point": "a", "op": ">", "value": 0}, {"point": "b", "op": ">", "value": 0}],
        then=[{"action": "log_event", "event_type": "fired"}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    assert engine.evaluate_cycle(stub, _results(a=1, b=0), now_ms=0) == ["r1"]


def test_else_branch_fires_when_condition_false(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        when=[{"point": "a", "op": ">", "value": 10}],
        then=[{"action": "set", "point": "out", "value": True}],
        else_=[{"action": "set", "point": "out", "value": False}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    engine.evaluate_cycle(stub, _results(a=1), now_ms=0)
    assert stub.writes == [("out", False)]


def test_disabled_rule_never_evaluated(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        enabled=False,
        when=[{"point": "a", "op": ">", "value": 0}],
        then=[{"action": "set", "point": "out", "value": True}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    fired = engine.evaluate_cycle(stub, _results(a=99), now_ms=0)
    assert fired == []
    assert stub.writes == []


def test_stale_point_never_matches_any_operator(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        when=[{"point": "a", "op": ">", "value": -1}],  # would trivially match any real number
        then=[{"action": "log_event", "event_type": "fired"}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    fired = engine.evaluate_cycle(stub, _stale("a"), now_ms=0)
    assert fired == []


def test_pulse_reverts_after_ms_not_before(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        when=[{"point": "btn", "op": "rising"}],
        then=[{"action": "pulse", "point": "led", "ms": 500}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    engine.evaluate_cycle(stub, _results(btn=False), now_ms=0)     # seed prior
    engine.evaluate_cycle(stub, _results(btn=True), now_ms=100)    # rising -> pulse ON
    assert stub.writes == [("led", True)]

    engine.evaluate_cycle(stub, _results(btn=True), now_ms=400)    # 300ms later: not due yet
    assert stub.writes == [("led", True)]

    engine.evaluate_cycle(stub, _results(btn=True), now_ms=650)    # 550ms later: due -> revert
    assert stub.writes == [("led", True), ("led", False)]


def test_pulse_supports_normally_closed_outputs(tmp_path):
    """value:false means the ACTIVE (pulsed-to) state is False — for a
    normally-closed output, pulsing means driving it LOW briefly, then
    reverting HIGH (the logical opposite of whatever was active), not the
    default True->False."""
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        when=[{"point": "btn", "op": "rising"}],
        then=[{"action": "pulse", "point": "nc_valve", "ms": 500, "value": False}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    engine.evaluate_cycle(stub, _results(btn=False), now_ms=0)
    engine.evaluate_cycle(stub, _results(btn=True), now_ms=100)   # pulse ACTIVE -> False
    assert stub.writes == [("nc_valve", False)]

    engine.evaluate_cycle(stub, _results(btn=True), now_ms=650)   # revert -> True
    assert stub.writes == [("nc_valve", False), ("nc_valve", True)]


def test_every_coil_write_is_logged(tmp_path):
    db_path = tmp_path / "e.db"
    init_db(db_path)
    engine = RuleEngine([_rule(
        when=[{"point": "btn", "op": "rising"}],
        then=[{"action": "set", "point": "out", "value": True}],
    )], IDENT, db_path)
    stub = StubPollEngine()

    engine.evaluate_cycle(stub, _results(btn=False), now_ms=0)
    engine.evaluate_cycle(stub, _results(btn=True), now_ms=100)

    events = fetch_events(db_path)
    coil_writes = [e for e in events if e["event_type"] == "rule_coil_write"]
    assert len(coil_writes) == 1
    for field in ("plant_id", "line_id", "zone_id", "station_id", "boot_id"):
        assert coil_writes[0][field] == IDENT[field]


# -- capstone: reproduce the reference's button->LED + fault logic purely
# from the migrated config, through the real poll+rule engines ------------

def _make_wired_engine(tmp_path, fake_client):
    io_config = load_seed("io_config.seed.v2.golden.json")
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    rule_engine = RuleEngine(io_config["rules"], IDENT, db_path)
    clients = {d["unit_id"]: fake_client for d in io_config["devices"]}
    poll_engine = PollEngine(io_config, IDENT, db_path, clients=clients, rule_engine=rule_engine)
    return poll_engine, db_path


def test_button_press_pulses_led_and_logs_maintenance_request(tmp_path):
    fake = FakeModbusClient()
    poll_engine, db_path = _make_wired_engine(tmp_path, fake)

    # debounce_ms=300 / poll_interval_ms=100 => 3 consecutive reads needed.
    poll_engine.run_cycle(now_ms=0)     # button unpressed, seeds debounce state False
    fake.coils[(1, 0)] = True
    poll_engine.run_cycle(now_ms=100)   # read 1 of 3 while pressed
    assert ("write_coil", 1, True, 1) not in fake.calls  # not debounced-through yet
    poll_engine.run_cycle(now_ms=200)   # read 2 of 3
    poll_engine.run_cycle(now_ms=300)   # read 3 of 3 -> debounced value flips True -> rising edge

    assert ("write_coil", 1, True, 1) in fake.calls  # LED coil pulsed on
    events = fetch_events(db_path)
    assert any(e["event_type"] == "maintenance_request" for e in events)

    # 500ms after the pulse fired (at ms=300) the LED must auto-revert.
    poll_engine.run_cycle(now_ms=850)
    assert ("write_coil", 1, False, 1) in fake.calls


def test_fault_register_logs_machine_fault(tmp_path):
    fake = FakeModbusClient()
    poll_engine, db_path = _make_wired_engine(tmp_path, fake)

    poll_engine.run_cycle(now_ms=0)          # fault_code=0, no fault
    events = fetch_events(db_path)
    assert not any(e["event_type"] == "machine_fault" for e in events)

    fake.registers[(2, 100)] = 7             # PLC reports a fault code
    poll_engine.run_cycle(now_ms=100)

    events = fetch_events(db_path)
    fault_events = [e for e in events if e["event_type"] == "machine_fault"]
    assert len(fault_events) == 1
    for field in ("plant_id", "line_id", "zone_id", "station_id", "boot_id"):
        assert fault_events[0][field] == IDENT[field]
