"""
Multi-zone deployment (Variant B) — `link.medium`. See
IO_Config_Execution_Plan.md's "Decisions locked in" / Link medium row:
`link.medium` (`"wired"` | `"wireless"`) is added per-zone, not per-site —
a single terminal can commission one zone on a wired switch and another
over a wireless gateway. It is "descriptive only to the engine": the
Modbus layer always speaks plain Modbus TCP/RTU and never inspects this
field or changes behavior because of it — see poll_engine.py, which never
imports this module.

What it DOES drive is commissioning: this module hands back the
recommended timeout/retry/poll defaults for a medium, for the
commissioning UI (or an installer script) to pre-fill when a zone's
`link.medium` is set — a wireless hop needs looser timeouts than a
switched wired segment on the same physical RTU hardware. Nothing in
engine/ enforces these numbers; a real config can override every one of
them (via bus.serial/bus.tcp and devices[].comms, per AR-08) regardless
of what `link.medium` says.
"""
from __future__ import annotations

RECOMMENDED_DEFAULTS: dict[str, dict[str, int]] = {
    "wired": {
        "timeout_ms": 800,
        "retries": 2,
        "backoff_ms": 200,
        "poll_interval_ms": 100,
    },
    "wireless": {
        "timeout_ms": 1500,
        "retries": 3,
        "backoff_ms": 400,
        "poll_interval_ms": 150,
    },
}


def recommended_comms_defaults(medium: str) -> dict[str, int]:
    """Recommended {timeout_ms, retries, backoff_ms, poll_interval_ms}
    for the given link medium. Raises ValueError on anything other than
    "wired"/"wireless" — there is no silent fallback, because guessing
    wrong here means an installer commissions a wireless zone with wired
    timeouts and then wonders why it drops out."""
    if medium not in RECOMMENDED_DEFAULTS:
        raise ValueError(f"unknown link medium: {medium!r}; expected 'wired' or 'wireless'")
    return dict(RECOMMENDED_DEFAULTS[medium])
