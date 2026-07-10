"""
Test Write — Phase 6. A manual output pulse for bench verification, gated
behind a commissioning-mode toggle + confirm + a fixed auto-timeout that
fires regardless of what the client does afterward — the plan's exact
requirement: "a dropped connection can never leave a test output energized"
(api_contract.md, POST /api/test/write).

This is deliberately a separate mechanism from RuleEngine's pulse action,
even though the revert-scheduling shape looks similar: a rule pulse is
config-driven and only exists because a rule fired; a test write is an
operator manually poking a specific coil on the bench and must work even
when zero rules are configured (which is exactly the state a fresh,
not-yet-configured unit is in during commissioning — the moment this
feature is needed most).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .event_store import log_event


@dataclass(frozen=True)
class TestWriteResult:
    ok: bool
    message: str = ""


class TestWriteManager:
    def __init__(self) -> None:
        self.commissioning_mode = False
        self._pending: list[dict] = []  # [{point, revert_at_ms, revert_value}]

    def set_commissioning_mode(self, enabled: bool) -> None:
        self.commissioning_mode = enabled
        # Deliberately NOT cancelling in-flight pending reverts when mode
        # is switched off mid-pulse — a scheduled revert-to-safe-state
        # completing on its own timer is always safe; cancelling it would
        # not be.

    def request_write(
        self, poll_engine, point_id: str, value: bool, *, confirm: bool, timeout_ms: int = 5000,
        now_ms: int, monotonic_ms: int | None = None,
    ) -> TestWriteResult:
        # AR-04: revert scheduling uses a monotonic clock, never the wall
        # clock. `now_ms` (wall clock) is only used elsewhere for stamping.
        # Defaults to now_ms when the caller doesn't pass one, so existing
        # callers/tests that only know about one "clock" keep working
        # unchanged — production PollEngine.run_cycle() supplies a real,
        # independent monotonic value.
        if monotonic_ms is None:
            monotonic_ms = now_ms
        if not self.commissioning_mode:
            return TestWriteResult(ok=False, message="commissioning mode is off")
        if not confirm:
            return TestWriteResult(ok=False, message="confirm is required for a manual output write")

        point = poll_engine._point_by_id.get(point_id)
        if point is None:
            return TestWriteResult(ok=False, message=f"unknown point: {point_id!r}")
        if point["kind"] != "digital_out":
            return TestWriteResult(ok=False, message=f"point {point_id!r} is not a digital_out")

        result = poll_engine.write_point(point_id, value)
        log_event(poll_engine.db_path, poll_engine.ident, "test_write", {
            "point": point_id, "value": value, "stale": result.stale, "error": result.error,
        })
        if result.stale:
            return TestWriteResult(ok=False, message=f"write failed: {result.error}")

        revert_value = point.get("safe_state", False)
        self._pending.append({
            "point": point_id, "revert_at_ms": monotonic_ms + timeout_ms, "revert_value": revert_value,
        })
        return TestWriteResult(ok=True, message=f"will auto-revert to {revert_value} in {timeout_ms}ms")

    def apply_pending_reverts(self, poll_engine, now_ms: int, monotonic_ms: int | None = None) -> None:
        if monotonic_ms is None:
            monotonic_ms = now_ms
        due, still_pending = [], []
        for pending in self._pending:
            (due if monotonic_ms >= pending["revert_at_ms"] else still_pending).append(pending)
        self._pending = still_pending
        for pending in due:
            result = poll_engine.write_point(pending["point"], pending["revert_value"])
            log_event(poll_engine.db_path, poll_engine.ident, "test_write_revert", {
                "point": pending["point"], "value": pending["revert_value"],
                "stale": result.stale, "error": result.error,
            })
