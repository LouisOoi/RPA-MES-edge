"""
Schema-migration framework.

`schema_version` (in io_config.json) is the product schema version and is
what drives OTA migration — distinct from `config_version`, which is the
per-save counter used for hot-reload/rollback (see execution plan, "Design
notes that matter").

Migrations are forward-only and registered by *source* version: MIGRATIONS[1]
takes a valid schema_version=1 document and returns a valid schema_version=2
document. `migrate_to_latest` chains them so a unit that's several releases
behind upgrades in one call. Callers (Phase 7 OTA flow) are responsible for
backing up the pre-migration file before calling this — this module is pure
and never touches disk.
"""
from __future__ import annotations


class MigrationError(Exception):
    pass


from .v1_to_v2 import migrate_v1_to_v2  # noqa: E402  (after MigrationError to avoid a cycle)

LATEST_SCHEMA_VERSION = 2

MIGRATIONS = {
    1: migrate_v1_to_v2,
}


def migrate_to_latest(doc: dict, *, updated_by: str) -> dict:
    version = doc.get("schema_version")
    if version is None:
        raise MigrationError("document has no schema_version")
    if version == LATEST_SCHEMA_VERSION:
        return doc
    if version > LATEST_SCHEMA_VERSION:
        raise MigrationError(
            f"document schema_version {version} is newer than this app "
            f"supports ({LATEST_SCHEMA_VERSION}); refusing to downgrade"
        )

    current = doc
    seen = set()
    while current["schema_version"] != LATEST_SCHEMA_VERSION:
        v = current["schema_version"]
        if v in seen:
            raise MigrationError(f"migration loop detected at schema_version {v}")
        seen.add(v)
        migrate_fn = MIGRATIONS.get(v)
        if migrate_fn is None:
            raise MigrationError(f"no migration registered for schema_version {v}")
        current = migrate_fn(current, updated_by=updated_by)
    return current
