"""
Multi-zone orchestrator (Deployment Variant B). See
IO_Config_Execution_Plan.md's "Decisions locked in" / Identity row: each
zone keeps its own independent identity (plant/line/zone/station + event
log) — N independent PollEngine instances, one per zone, each with its
own ctrl_id.json/system_config.json/io_config.json/event DB, running
inside ONE process (the plan specs a Windows Service; this orchestrator
is plain, OS-agnostic Python that doesn't care what process hosts it —
see service/windows_service.py for the Windows-specific host that starts
one of these and stops it on service shutdown).

The one property this module exists to guarantee, stated explicitly in
the plan: "each supervised on its own thread so one zone's fault or
wireless dropout doesn't affect the others." Concretely:
  - Each zone's PollEngine.run_cycle() runs on its own dedicated thread,
    on its own schedule (that zone's bus.poll_interval_ms) — a slow or
    stuck zone never delays another zone's cycle.
  - An exception raised by one zone's run_cycle() is caught, logged, and
    that zone's thread restarts with backoff — it never propagates and
    never touches any other zone's ZoneHandle or thread.
  - Zones are added/removed/looked up by zone_id; nothing here assumes a
    fixed N.

Each zone reuses PollEngine/RuleEngine exactly as built for the
single-terminal (Variant A) deployment — this module only supervises
existing, already-tested engine instances; it does not change how any
one zone works.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .event_store import log_event


@dataclass
class ZoneHandle:
    """Everything the orchestrator tracks for one registered zone."""
    zone_id: str
    engine: Any  # PollEngine
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    crash_count: int = 0
    last_error: str | None = None
    running: bool = False


def _zone_loop(
    zone_id: str, engine: Any, stop_event: threading.Event,
    *, on_error: Callable[[str, Exception], None], max_cycles: int | None = None,
) -> int:
    """The per-zone supervised loop body — deliberately a plain
    function, not a method, so tests can call it directly (with
    max_cycles set, and a stop_event they control) instead of spinning
    up a real background thread and racing a sleep timer. Returns the
    number of cycles actually completed.

    A run_cycle() exception is caught and reported HERE via `on_error`,
    then RE-RAISED — the thread this runs on is meant to end on a crash;
    ZoneOrchestrator._supervise() is what decides whether/how to restart
    it. This function only ever touches the one zone it was given."""
    poll_interval_ms = engine.io_config["bus"]["poll_interval_ms"]
    cycles = 0
    while not stop_event.is_set():
        if max_cycles is not None and cycles >= max_cycles:
            break
        try:
            engine.run_cycle()
        except Exception as exc:  # noqa: BLE001 - must never propagate past this zone's thread
            on_error(zone_id, exc)
            raise
        cycles += 1
        if stop_event.wait(poll_interval_ms / 1000):
            break
    return cycles


class ZoneOrchestrator:
    """Owns N PollEngine instances, one per zone, each on its own
    supervised thread. This class IS the "multi-zone orchestrator" the
    plan calls for; it is plain Python with no OS-specific code."""

    def __init__(self, *, restart_backoff_s: float = 1.0, max_restarts: int | None = None) -> None:
        self._zones: dict[str, ZoneHandle] = {}
        self._restart_backoff_s = restart_backoff_s
        self._max_restarts = max_restarts

    def add_zone(self, zone_id: str, engine: Any) -> None:
        """Registers an already-constructed PollEngine for `zone_id`
        (built the exact same way a single-terminal Variant-A engine is
        — this orchestrator never constructs an engine itself). Does not
        start it; call start_zone() or start_all() separately."""
        if zone_id in self._zones:
            raise ValueError(f"zone '{zone_id}' is already registered")
        self._zones[zone_id] = ZoneHandle(zone_id=zone_id, engine=engine)

    def remove_zone(self, zone_id: str, *, timeout_s: float = 5.0) -> None:
        if zone_id not in self._zones:
            return
        if self._zones[zone_id].running:
            self.stop_zone(zone_id, timeout_s=timeout_s)
        del self._zones[zone_id]

    def get_engine(self, zone_id: str) -> Any:
        return self._zones[zone_id].engine

    def zone_ids(self) -> list[str]:
        return list(self._zones.keys())

    def is_running(self, zone_id: str) -> bool:
        return self._zones[zone_id].running

    def crash_count(self, zone_id: str) -> int:
        return self._zones[zone_id].crash_count

    def last_error(self, zone_id: str) -> str | None:
        return self._zones[zone_id].last_error

    def start_zone(self, zone_id: str) -> None:
        handle = self._zones[zone_id]
        if handle.running:
            return
        handle.stop_event.clear()
        handle.running = True
        handle.thread = threading.Thread(
            target=self._supervise, args=(handle,), name=f"zone-{zone_id}", daemon=True,
        )
        handle.thread.start()

    def start_all(self) -> None:
        for zone_id in self.zone_ids():
            self.start_zone(zone_id)

    def stop_zone(self, zone_id: str, *, timeout_s: float = 5.0) -> None:
        handle = self._zones[zone_id]
        handle.stop_event.set()
        if handle.thread is not None:
            handle.thread.join(timeout=timeout_s)
        handle.running = False

    def stop_all(self, *, timeout_s: float = 5.0) -> None:
        for zone_id in self.zone_ids():
            self.stop_zone(zone_id, timeout_s=timeout_s)

    def _on_error(self, zone_id: str, exc: Exception) -> None:
        handle = self._zones[zone_id]
        handle.crash_count += 1
        handle.last_error = str(exc)
        try:
            log_event(handle.engine.db_path, handle.engine.ident, "zone_thread_crashed", {
                "zone_id": zone_id, "crash_count": handle.crash_count, "error": str(exc),
            })
        except Exception:  # noqa: BLE001 - logging the crash must never itself crash the supervisor
            pass

    def _supervise(self, handle: ZoneHandle) -> None:
        """Runs on the zone's dedicated thread for its entire lifetime.
        Restarts _zone_loop with backoff after any crash, up to
        max_restarts (None = unlimited); stops for good on an explicit
        stop_zone() or once the restart budget is exhausted. Nothing
        here can reach any other zone's ZoneHandle or thread — each
        _supervise() call only ever touches the one `handle` it owns."""
        while not handle.stop_event.is_set():
            try:
                _zone_loop(handle.zone_id, handle.engine, handle.stop_event, on_error=self._on_error)
                break  # clean stop (stop_event was set) — not a crash
            except Exception:  # noqa: BLE001 - already recorded by _on_error above
                if self._max_restarts is not None and handle.crash_count > self._max_restarts:
                    break
                if handle.stop_event.wait(self._restart_backoff_s):
                    break
                continue
        handle.running = False
