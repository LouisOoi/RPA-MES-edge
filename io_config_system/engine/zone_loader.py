"""
Loads a Deployment-Variant-B zone directory into a live PollEngine + its
resource paths. See engine/zone_orchestrator.py's module docstring: each
zone is its own independent ctrl_id.json/system_config.json/io_config.json
/event_log.db — this is the same four-file shape a single-terminal
Variant-A deployment already uses (see tests/test_api_phase5.py's
app_ctx fixture), just N of them, one directory per zone, e.g.:

    zones/weld_cell/
        ctrl_id.json
        system_config.json
        io_config.json
        event_log.db          (created here if missing)

A zone directory is not a new file format — it's the existing format,
laid out N times.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config_store
from .event_store import init_db
from .poll_engine import PollEngine
from .rule_engine import RuleEngine

REQUIRED_FILES = ("ctrl_id.json", "system_config.json", "io_config.json")


class ZoneDirectoryError(Exception):
    pass


def load_zone_from_directory(
    zone_dir: str | Path, zone_id: str, *,
    clients_factory: Any | None = None, rule_engine_enabled: bool = True,
) -> tuple[PollEngine, dict[str, Path]]:
    """Returns (engine, resource_paths) for one zone.

    `clients_factory` defaults to None, meaning PollEngine builds real
    pymodbus clients from the zone's own io_config (production path).
    Tests pass a fake-client factory here instead — never a network
    dependency to load a zone directory, only to actually poll it.

    `resource_paths` is a plain dict (identity_path/system_path/
    io_config_path) rather than api/multi_zone_app.py's ZoneResources
    class, so engine/ doesn't import anything from api/ — the caller
    (a Windows Service entry point, or a test) wraps this dict into
    whatever the HTTP layer needs."""
    zone_dir = Path(zone_dir)
    for filename in REQUIRED_FILES:
        if not (zone_dir / filename).exists():
            raise ZoneDirectoryError(f"zone '{zone_id}' at {zone_dir} is missing {filename}")

    identity_path = zone_dir / "ctrl_id.json"
    system_path = zone_dir / "system_config.json"
    io_config_path = zone_dir / "io_config.json"
    db_path = zone_dir / "event_log.db"

    ident = config_store.read_json(identity_path)
    io_config = config_store.read_json(io_config_path)
    init_db(db_path)

    rule_engine = RuleEngine(io_config.get("rules", []), ident, db_path) if rule_engine_enabled else None
    engine = PollEngine(
        io_config, ident, db_path, config_path=io_config_path,
        rule_engine=rule_engine, clients_factory=clients_factory,
    )

    resource_paths = {
        "identity_path": identity_path, "system_path": system_path, "io_config_path": io_config_path,
    }
    return engine, resource_paths


def load_all_zones(
    zones_root: str | Path, *, clients_factory: Any | None = None,
) -> dict[str, tuple[PollEngine, dict[str, Path]]]:
    """Discovers every immediate subdirectory of `zones_root` as one
    zone (subdirectory name == zone_id) and loads each via
    load_zone_from_directory(). A single zone failing to load raises
    immediately, naming that zone — a commissioning mistake in one
    zone's directory is worth stopping the whole service startup for,
    not silently skipping that zone and running short-handed."""
    zones_root = Path(zones_root)
    result: dict[str, tuple[PollEngine, dict[str, Path]]] = {}
    for entry in sorted(zones_root.iterdir()):
        if not entry.is_dir():
            continue
        result[entry.name] = load_zone_from_directory(entry, entry.name, clients_factory=clients_factory)
    return result
