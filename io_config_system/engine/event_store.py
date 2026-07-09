"""
Mirrors reference §6 controller/event_store.py: append-only SQLite
event_log, log_event() stamping all 6 identity fields on every row. DB path
is injectable (reference hardcodes /var/db/event_log.db; tests use a tmp
path).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id     TEXT    NOT NULL,
    line_id      TEXT    NOT NULL,
    zone_id      TEXT    NOT NULL,
    station_id   TEXT    NOT NULL,
    boot_id      TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    synced       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_unsynced ON event_log(synced, seq);
"""


def init_db(db_path: str | Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def log_event(db_path: str | Path, ident: dict, event_type: str, payload: dict) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO event_log
               (plant_id, line_id, zone_id, station_id, boot_id, event_type, payload, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                ident["plant_id"], ident["line_id"],
                ident["zone_id"], ident["station_id"],
                ident["boot_id"], event_type,
                json.dumps(payload), int(time.time() * 1000),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_events(db_path: str | Path) -> list[dict]:
    """Test/inspection helper — not part of the production data path."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM event_log ORDER BY seq").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
