"""
AR-07 — permit-to-edit gate for actuating rule changes. See
IO_Config_Execution_Plan.md's "Decisions locked in" / Design notes (Config
data model): non-actuating config keeps instant hot-reload; a config save
that touches any owner:"edge" output's RULE WIRING requires an explicit
operator acknowledgement of the resulting output states before it takes
effect.

Scope is deliberately narrow, per the plan's own wording ("rule wiring"):
this looks only at what rules would DO to edge-owned outputs — which rule,
which action, targeting which point, with what value/duration. Renaming a
point, retuning a debounce_ms, adding a read-only telemetry point, or
changing a rule that only targets a plc-owned point (which edge rules
can't actually drive anyway, per AR-03) all stay on the instant-hot-reload
path. This is intentionally conservative in the "stays gated" direction:
if in doubt about whether something counts as rule wiring, it's cheaper
for an operator to acknowledge an unnecessary permit prompt than for a
real actuating change to slip through ungated.
"""
from __future__ import annotations

ACTUATING_ACTIONS = ("set", "pulse")


def _edge_output_ids(io_config: dict) -> set[str]:
    return {
        p["id"] for p in io_config.get("points", [])
        if p.get("kind") == "digital_out" and p.get("owner") == "edge"
    }


def _actuating_rule_wiring(io_config: dict) -> frozenset:
    """A hashable projection of "what every enabled rule would do to an
    edge-owned output": (rule_id, branch, action, point, value, ms). Two
    configs with an identical projection are, for AR-07's purposes,
    identical in what they can actuate — even if everything else about
    them (names, comments, non-edge points, disabled rules) differs."""
    edge_outputs = _edge_output_ids(io_config)
    entries = []
    for rule in io_config.get("rules", []):
        if not rule.get("enabled", True):
            continue
        for branch in ("then", "else"):
            for action in rule.get(branch, []):
                if action.get("action") not in ACTUATING_ACTIONS:
                    continue
                point_id = action.get("point")
                if point_id not in edge_outputs:
                    continue
                entries.append((
                    rule.get("id"), branch, action.get("action"), point_id,
                    action.get("value"), action.get("ms"),
                ))
    return frozenset(entries)


def is_actuating_change(old_io_config: dict, new_io_config: dict) -> bool:
    """True if `new_io_config` changes what any owner:'edge' output's
    rule wiring would actually DO, relative to `old_io_config`. This is
    the AR-07 gate check: an actuating change may not hot-reload without
    an explicit acknowledgement (PollEngine.reload(permit_acknowledged=)).
    """
    return _actuating_rule_wiring(old_io_config) != _actuating_rule_wiring(new_io_config)


def resulting_output_states(io_config: dict) -> dict[str, dict]:
    """What an operator acknowledging an actuating change should be shown
    before it takes effect: for every owner:'edge' output, its configured
    `safe_state` (what reload() drives it to immediately — see
    poll_engine.py's "safe_state before swap" behavior), its
    `output_class`, and whether any enabled rule can still write it going
    forward under the NEW config."""
    edge_output_ids = _edge_output_ids(io_config)
    points_by_id = {p["id"]: p for p in io_config.get("points", []) if p["id"] in edge_output_ids}
    driven_by_rule = {entry[3] for entry in _actuating_rule_wiring(io_config)}
    return {
        point_id: {
            "safe_state": point.get("safe_state", False),
            "output_class": point.get("output_class"),
            "driven_by_rule": point_id in driven_by_rule,
        }
        for point_id, point in points_by_id.items()
    }
