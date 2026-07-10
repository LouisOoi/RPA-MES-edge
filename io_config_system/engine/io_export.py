"""
Export / import — Phase 6. "A validated config can be exported and
imported onto identical terminals" (plan, "Config portability").

Scope is `io_config.json` only — bus/devices/points/rules. Identity
(plant/line/zone/station/boot_id) and system config (network/broker/time)
are deliberately excluded: those are per-terminal facts, not part of "a
good IO setup" being cloned onto a second, physically identical unit. If a
customer wants to clone those too, that's a different, explicit action —
this module must not make that decision for them by silently bundling it.

Import re-validates and goes through PollEngine.reload()'s normal
validate-then-swap path — an imported config gets no special trust just
because it was exported from a working unit. The exporting unit's
config_version/updated_at/updated_by are stripped on export and re-stamped
fresh on import, because those describe *when and by whom this specific
terminal's config was last saved*, not a property of the IO setup itself.
"""
from __future__ import annotations

import time


def export_io_config(io_config: dict) -> dict:
    exported = {k: v for k, v in io_config.items() if k not in ("config_version", "updated_at", "updated_by")}
    return exported


def build_import_doc(exported: dict, *, current_config_version: int, updated_by: str) -> dict:
    """Takes what export_io_config() produced (or an identical hand-edited
    file) and re-stamps it as a new save on THIS terminal, ready to hand to
    PollEngine.reload(). Rejects an exported doc that still carries the
    stripped fields with mismatched types rather than silently overwriting
    something unexpected."""
    for field in ("config_version", "updated_at", "updated_by"):
        if field in exported:
            raise ValueError(
                f"import doc unexpectedly already has {field!r} — re-export "
                f"with export_io_config() rather than hand-crafting this"
            )
    return {
        **exported,
        "config_version": current_config_version + 1,
        "updated_at": int(time.time() * 1000),
        "updated_by": updated_by,
    }
