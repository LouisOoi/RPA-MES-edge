"""
Admin-tier identity edits — Phase 5.

`boot_id` is the one field in this whole system that no HTTP request is
ever allowed to change (reference Rule 2 / plan §"Identity"). That
constraint is enforced HERE, in code, not left to "the API just won't
expose a field for it" — a caller handing us `{"boot_id": "..."}` in the
update dict gets an exception, not a silent ignore, so a bug elsewhere
that forwards the whole request body can't quietly slip a new boot_id in.

Every successful edit is logged as an `identity_change` event (never
silent, per the plan) with old and new values, so the history fork this
creates is traceable on the server side.
"""
from __future__ import annotations

from pathlib import Path

from validators import ConfigValidationError, validate_identity

from . import config_store
from .event_store import log_event

EDITABLE_FIELDS = ("plant_id", "line_id", "zone_id", "station_id")


class IdentityUpdateError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def update_identity(
    identity_path: str | Path,
    db_path: str | Path,
    updates: dict,
    *,
    updated_by: str,
    confirm: bool,
) -> dict:
    """Applies `updates` (a subset of plant_id/line_id/zone_id/station_id)
    on top of the identity currently on disk. Returns the new identity
    dict on success. Raises IdentityUpdateError and leaves the on-disk
    file untouched on any rejection."""
    problems = []

    if "boot_id" in updates:
        problems.append("boot_id is system-locked and can never be edited through this API")

    unknown_fields = set(updates) - set(EDITABLE_FIELDS) - {"boot_id"}
    if unknown_fields:
        problems.append(f"unknown identity field(s): {sorted(unknown_fields)}")

    if not confirm:
        problems.append("confirm must be true — identity changes break historical continuity")

    if problems:
        raise IdentityUpdateError(problems)

    current = config_store.read_json(identity_path)
    new_identity = {**current, **{k: v for k, v in updates.items() if k in EDITABLE_FIELDS}}
    # boot_id is carried forward from `current` untouched, never from `updates`
    new_identity["boot_id"] = current["boot_id"]

    try:
        validate_identity(new_identity)
    except ConfigValidationError as exc:
        raise IdentityUpdateError(exc.problems) from exc

    config_store.atomic_write_json(identity_path, new_identity)

    changed = {
        field: {"old": current.get(field), "new": new_identity.get(field)}
        for field in EDITABLE_FIELDS
        if current.get(field) != new_identity.get(field)
    }
    # Stamped with the OLD identity, deliberately: this event is the last
    # thing filed under the identity that's about to stop being used, so
    # whoever is watching that station's existing stream sees the fork
    # explicitly instead of the trail just going quiet. `changed` in the
    # payload carries both old and new values regardless.
    log_event(db_path, current, "identity_change", {
        "updated_by": updated_by,
        "changed": changed,
    })

    return new_identity
