"""
Per-unit OTA status — Phase 7's "per-unit update status reported
centrally." The events `apply_and_reload` already logs via
`event_store.log_event` ARE the central-reporting path — that's the same
SQLite-event-log -> sync-agent -> server pipeline every other event in
this codebase already uses, per the reference architecture. This module
is just the fast local answer to "what's my current OTA state right now"
without scanning the whole event log — a status file, kept current.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import config_store


def record_ota_status(path: str | Path, *, ok: bool, rolled_back: bool, config_version, problems: list[str]) -> None:
    config_store.atomic_write_json(path, {
        "ok": ok,
        "rolled_back": rolled_back,
        "config_version": config_version,
        "problems": problems,
        "checked_at": int(time.time() * 1000),
    })


def read_ota_status(path: str | Path) -> dict | None:
    try:
        return config_store.read_json(path)
    except FileNotFoundError:
        return None
