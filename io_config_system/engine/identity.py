"""
Mirrors reference §5 controller/identity.py exactly, with the file path
injectable instead of hardcoded to /etc/ctrl_id.json (needed for tests, and
for the fact this whole codebase doesn't run as root on a real Pi yet).

boot_id is generated here ONLY when the identity file does not exist at
all — i.e. genuine first boot / factory reset. This module must never be
called from a code path that could run again on an already-provisioned
unit and overwrite an existing boot_id; the admin-tier identity endpoints
(Phase 5) load and rewrite plant/line/zone/station only, never touching
this generation path (reference Rule 2).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path


def load_identity(path: str | Path, *, provisioning_defaults: dict | None = None) -> dict:
    id_file = Path(path)
    if id_file.exists():
        return json.loads(id_file.read_text())

    if provisioning_defaults is None:
        raise FileNotFoundError(
            f"{id_file} does not exist and no provisioning_defaults were given — "
            f"first-boot provisioning (Phase 1 wizard) must supply plant/line/zone/station"
        )

    ident = {
        **provisioning_defaults,
        "boot_id": str(uuid.uuid4()),
    }
    id_file.parent.mkdir(parents=True, exist_ok=True)
    id_file.write_text(json.dumps(ident, indent=2))
    return ident
