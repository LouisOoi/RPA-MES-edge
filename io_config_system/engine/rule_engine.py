"""
Rule engine — Phase 3. Evaluates `rules[]` each poll cycle against the
*effective* (debounced) values the poll engine just produced. This is what
makes IO behaviour data instead of code: the button->LED and fault->event
logic that used to be hardcoded in modbus_poll.py now lives entirely in
io_config.json, evaluated here by a fixed, whitelisted interpreter.

Hard constraints from the plan, enforced here (not just by convention):
  - No eval(). Every operator and action is an explicit branch below; an
    unrecognized one is a bug in validators.py (which is supposed to have
    already rejected it), not something this engine improvises around.
  - A condition on a stale point never matches. A dropped read must not be
    able to accidentally fire (or accidentally NOT fire) a rule by being
    silently treated as some default value.
  - Edge operators (rising/falling) require a WITNESSED prior value. On the
    very first cycle a point is seen, there is no prior state, so rising/
    falling cannot fire — this prevents every rule from spuriously firing
    the moment the engine starts up just because a value happens to already
    be true.
  - Every coil write this engine performs (set/pulse) is logged via
    event_store.log_event, satisfying the plan's "every coil write logged".
"""
from __future__ import annotations

from typing import Any

from .event_store import log_event
from .point_io import ReadResult

_COMPARISON_OPS = {
    ">":  lambda value, cond: value > cond["value"],
    "<":  lambda value, cond: value < cond["value"],
    ">=": lambda value, cond: value >= cond["value"],
    "<=": lambda value, cond: value <= cond["value"],
    "==": lambda value, cond: value == cond["value"],
    "between": lambda value, cond: cond["low"] <= value <= cond["high"],
}
_EDGE_OPS = {"rising", "falling"}
_ALL_OPS = set(_COMPARISON_OPS) | _EDGE_OPS


class RuleEngine:
    def __init__(self, rules: list[dict], ident: dict, db_path) -> None:
        self.rules = rules
        self.ident = ident
        self.db_path = db_path
        self._prev_values: dict[str, Any] = {}
        self._pending_reverts: list[dict] = []  # [{point, revert_at_ms, rule_id}]

    def evaluate_cycle(self, poll_engine, results: dict[str, ReadResult], *, now_ms: int) -> list[str]:
        """Runs pending pulse-reverts, then evaluates every enabled rule
        once. Returns the ids of rules whose `when` matched (then-branch
        fired) this cycle."""
        self._apply_pending_reverts(poll_engine, now_ms)

        current_values = {pid: r.value for pid, r in results.items()}
        stale = {pid: r.stale for pid, r in results.items()}

        fired: list[str] = []
        for rule in self.rules:
            if not rule.get("enabled", True):
                continue
            if self._evaluate_when(rule, current_values, stale):
                self._run_actions(poll_engine, rule["id"], rule.get("then", []), now_ms)
                fired.append(rule["id"])
            else:
                self._run_actions(poll_engine, rule["id"], rule.get("else", []), now_ms)

        # Edge detection needs the value that was actually current THIS
        # cycle to become "previous" for next cycle — update after
        # evaluating, so a rule can't see its own cycle's transition twice.
        self._prev_values.update(current_values)
        return fired

    # -- condition evaluation -------------------------------------------------

    def _evaluate_when(self, rule: dict, current_values: dict, stale: dict) -> bool:
        match = rule.get("match", "all")
        outcomes = [self._evaluate_condition(c, current_values, stale) for c in rule["when"]]
        return all(outcomes) if match == "all" else any(outcomes)

    def _evaluate_condition(self, cond: dict, current_values: dict, stale: dict) -> bool:
        point_id = cond["point"]
        op = cond["op"]
        if op not in _ALL_OPS:
            raise ValueError(f"unwhitelisted operator: {op!r}")  # validators.py should have caught this already

        if stale.get(point_id, True) or point_id not in current_values:
            return False  # never act on a reading we don't trust

        value = current_values[point_id]
        if value is None:
            return False

        if op == "rising":
            prev = self._prev_values.get(point_id)
            return prev is False and value is True
        if op == "falling":
            prev = self._prev_values.get(point_id)
            return prev is True and value is False
        return _COMPARISON_OPS[op](value, cond)

    # -- actions ----------------------------------------------------------------

    def _run_actions(self, poll_engine, rule_id: str, actions: list[dict], now_ms: int) -> None:
        for action in actions:
            kind = action["action"]
            if kind == "set":
                self._write_and_log(poll_engine, rule_id, action["point"], action["value"])
            elif kind == "pulse":
                # `value` is the ACTIVE (pulsed-to) state, default True for
                # backward compatibility with configs written before this
                # field existed. Normally-closed outputs set value:false to
                # pulse LOW and revert HIGH; the revert is always the
                # logical opposite of whatever the active state was.
                active_value = action.get("value", True)
                self._write_and_log(poll_engine, rule_id, action["point"], active_value)
                self._pending_reverts.append({
                    "point": action["point"],
                    "revert_at_ms": now_ms + action["ms"],
                    "rule_id": rule_id,
                    "revert_value": not active_value,
                })
            elif kind == "log_event":
                log_event(self.db_path, self.ident, action["event_type"], {"rule_id": rule_id})
            else:
                raise ValueError(f"unwhitelisted action: {kind!r}")  # validators.py should have caught this already

    def _apply_pending_reverts(self, poll_engine, now_ms: int) -> None:
        due, still_pending = [], []
        for pending in self._pending_reverts:
            (due if now_ms >= pending["revert_at_ms"] else still_pending).append(pending)
        self._pending_reverts = still_pending
        for pending in due:
            self._write_and_log(poll_engine, pending["rule_id"], pending["point"], pending["revert_value"])

    def _write_and_log(self, poll_engine, rule_id: str, point_id: str, value: bool) -> None:
        result = poll_engine.write_point(point_id, value)
        log_event(self.db_path, self.ident, "rule_coil_write", {
            "rule_id": rule_id, "point": point_id, "value": value,
            "stale": result.stale, "error": result.error,
        })
