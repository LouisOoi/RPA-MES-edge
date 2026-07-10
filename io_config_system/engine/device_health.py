"""
AR-08 — per-slave comms tuning + graceful degradation for a dead device.

The finding: "RTU polling doesn't degrade gracefully." Today every poll
cycle spends the same timeout*retries budget probing every device
regardless of whether it's healthy — a single dead RTU slave on a shared
serial bus stalls every cycle waiting out its timeout, on every point it
owns, forever, with no way to tell "genuinely offline" apart from "having
one bad cycle."

Two pieces:
  - `resolve_comms()`: gives every device — RTU or TCP — a concrete,
    per-slave {timeout_ms, retries, backoff_ms, mark_dead_after_failures,
    dead_rescan_ms}, falling back from a device-level `comms` override to
    the transport-level bus default to a hardcoded last resort. This is
    what makes "RTU gains the timeout/retry fields TCP already had"
    (AR-08) a genuinely per-slave setting, not just one shared bus-wide
    number applied uniformly.
  - `DeviceHealthTracker`: cycle-counted (no wall-clock dependency, same
    style as debounce.py) — marks a device "dead" after
    `mark_dead_after_failures` consecutive failed poll attempts, then
    only re-probes it every `dead_rescan_ms` (converted to a cycle count
    via the bus's poll_interval_ms) instead of every single cycle. A
    live device is unaffected: it's probed every cycle exactly as before.
    Recovering (one successful probe) immediately clears dead state and
    resumes full-rate polling — this is a liveness gate, not a
    permanent quarantine.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MARK_DEAD_AFTER_FAILURES = 3
DEFAULT_DEAD_RESCAN_MS = 5000
DEFAULT_TIMEOUT_MS = 800
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_MS = 100


def resolve_comms(bus: dict, device: dict) -> dict:
    transport_defaults = bus.get("serial", {}) if bus.get("transport") == "rtu" else bus.get("tcp", {})
    device_overrides = device.get("comms", {})
    return {
        "timeout_ms": device_overrides.get("timeout_ms", transport_defaults.get("timeout_ms", DEFAULT_TIMEOUT_MS)),
        "retries": device_overrides.get("retries", transport_defaults.get("retries", DEFAULT_RETRIES)),
        "backoff_ms": device_overrides.get("backoff_ms", transport_defaults.get("backoff_ms", DEFAULT_BACKOFF_MS)),
        "mark_dead_after_failures": device_overrides.get("mark_dead_after_failures", DEFAULT_MARK_DEAD_AFTER_FAILURES),
        "dead_rescan_ms": device_overrides.get("dead_rescan_ms", DEFAULT_DEAD_RESCAN_MS),
    }


@dataclass
class _DeviceState:
    consecutive_failures: int = 0
    dead: bool = False
    cycles_since_probe: int = 0


class DeviceHealthTracker:
    """One instance per PollEngine, keyed by the config-level `unit_id`.
    Entirely cycle-counted so tests can drive it deterministically one
    run_cycle() at a time, with no dependency on real elapsed time."""

    def __init__(self) -> None:
        self._state: dict[int, _DeviceState] = {}

    def should_probe_this_cycle(self, unit_id: int, comms: dict, poll_interval_ms: int) -> bool:
        """True if this device should actually be read this cycle. A
        live (or never-seen) device is always probed. A dead device is
        only probed once every `dead_rescan_ms` worth of cycles — the
        cycle counter, not a clock, so this stays deterministic in
        tests and immune to wall-clock steps (AR-04's concern, reused
        here on principle)."""
        state = self._state.setdefault(unit_id, _DeviceState())
        if not state.dead:
            return True
        rescan_cycles = max(1, comms["dead_rescan_ms"] // max(poll_interval_ms, 1))
        state.cycles_since_probe += 1
        if state.cycles_since_probe >= rescan_cycles:
            state.cycles_since_probe = 0
            return True
        return False

    def record_result(self, unit_id: int, comms: dict, *, ok: bool) -> bool:
        """Records whether this cycle's probe (if one happened) succeeded.
        Returns True if this call is the transition INTO dead state (for
        the caller to log an event on), False otherwise — including when
        the device was already dead or already healthy."""
        state = self._state.setdefault(unit_id, _DeviceState())
        was_dead = state.dead
        if ok:
            state.consecutive_failures = 0
            state.dead = False
            state.cycles_since_probe = 0
        else:
            state.consecutive_failures += 1
            if state.consecutive_failures >= comms["mark_dead_after_failures"]:
                state.dead = True
        return (not was_dead) and state.dead

    def is_dead(self, unit_id: int) -> bool:
        return self._state.get(unit_id, _DeviceState()).dead
