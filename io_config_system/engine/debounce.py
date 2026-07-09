"""
Digital-input debounce.

Deliberate deviation from the reference script, not a literal port: the
reference's modbus_poll.py claims "300 ms SW debounce for button coil" in
prose (RS485 spec table) but its actual code only increments `btn_count` on
a fresh rising edge (`if btn and not prev_button: btn_count += 1`) and
resets to 0 on every single cycle where the button isn't *freshly* rising
— it never accumulates across a held signal. That code cannot reach
`DEBOUNCE_COUNT` (3) under a single press; it counts separate edges, not a
sustained state. It doesn't do what the surrounding prose says it does.

This implements what the plan and the reference's prose actually describe:
a value is only accepted as changed once the raw read has been consistently
the new value for `debounce_ms / poll_interval_ms` consecutive cycles.
"""
from __future__ import annotations


class Debouncer:
    def __init__(self, poll_interval_ms: int) -> None:
        self._poll_interval_ms = poll_interval_ms
        self._state: dict[str, dict] = {}

    def apply(self, point_id: str, debounce_ms: int, raw_value: bool) -> bool:
        """Returns the debounced (effective) value for this point. First
        call for a point seeds the stable value directly from the raw
        read — there is no "prior state" to debounce against on startup,
        so no phantom transition is invented."""
        required_reads = max(1, debounce_ms // self._poll_interval_ms)
        state = self._state.get(point_id)
        if state is None:
            state = {"stable": raw_value, "candidate": raw_value, "count": 0}
            self._state[point_id] = state
            return state["stable"]

        if raw_value == state["stable"]:
            state["candidate"] = state["stable"]
            state["count"] = 0
            return state["stable"]

        if raw_value == state["candidate"]:
            state["count"] += 1
        else:
            state["candidate"] = raw_value
            state["count"] = 1

        if state["count"] >= required_reads:
            state["stable"] = raw_value
            state["count"] = 0

        return state["stable"]
