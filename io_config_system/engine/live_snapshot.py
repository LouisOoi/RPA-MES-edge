"""
Thread-safe live-values snapshot. This is the "read-only live-values
snapshot" the architecture diagram shows the web app reading from — it is
the ONLY thing the Flask process and the poll engine share besides the
config file, per the plan's "browser crash never stalls Modbus" guarantee.
GET /api/live (Phase 6) reads this directly; it must never block on IO.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class LiveSnapshot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._points: dict[str, dict[str, Any]] = {}

    def update(self, point_id: str, value: Any, stale: bool) -> None:
        with self._lock:
            self._points[point_id] = {
                "value": value,
                "stale": stale,
                "ts": int(time.time() * 1000),
            }

    def get(self, point_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._points.get(point_id)
            return dict(entry) if entry else None

    def as_dict(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {pid: dict(v) for pid, v in self._points.items()}
