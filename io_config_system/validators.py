"""
Phase 0 — config validators.

Two layers, deliberately kept separate:

1. Structural validation (JSON Schema) — shape, types, required fields.
   Lives in schemas/*.json. Rarely changes; changing it is a schema_version bump.

2. Business-rule validation (this module) — policy checks that are cheap to
   change without a schema migration: the rule-engine v1 single-condition cap,
   the analog_in feature gate, dangling point references, and output
   contention (two rules writing the same coil). Per the execution plan,
   lifting the v1 caps later is a validator change, not a schema migration.

Raises ConfigValidationError with a list of human-readable problems; never
partially applies a config. Callers must treat "raises" as "reject this
config, keep the running one" (Phase 4 hot-reload semantics).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_DIR = Path(__file__).parent / "schemas"

_SCHEMA_CACHE: dict[str, dict] = {}


class ConfigValidationError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _load_schema(name: str) -> dict:
    if name not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[name] = json.loads((SCHEMA_DIR / name).read_text())
    return _SCHEMA_CACHE[name]


def validate_identity(doc: dict) -> None:
    schema = _load_schema("identity.schema.json")
    _run_jsonschema(doc, schema)


def validate_system(doc: dict) -> None:
    schema = _load_schema("system.schema.json")
    _run_jsonschema(doc, schema)


def validate_io(doc: dict, *, enforce_v1_policy_caps: bool = True) -> None:
    """Validate an io_config document end to end.

    `enforce_v1_policy_caps=True` (the default, and the only mode the live
    product uses today) additionally enforces the rule-engine v1 and analog
    v1 policy caps described in the execution plan. It is a parameter, not a
    hardcoded assumption, specifically so a later release can flip it off
    without touching this function's structural checks.
    """
    version = doc.get("schema_version")
    if version == 1:
        schema = _load_schema("io_v1.schema.json")
        _run_jsonschema(doc, schema)
        return  # v1 predates the rule engine; no business rules to check
    if version == 2:
        schema = _load_schema("io_v2.schema.json")
        _run_jsonschema(doc, schema)
        _run_io_v2_business_rules(doc, enforce_v1_policy_caps=enforce_v1_policy_caps)
        return

    raise ConfigValidationError([f"unsupported schema_version: {version!r}"])


def _run_jsonschema(doc: dict, schema: dict) -> None:
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    problems = [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]
    if problems:
        raise ConfigValidationError(problems)


def _run_io_v2_business_rules(doc: dict, *, enforce_v1_policy_caps: bool) -> None:
    problems: list[str] = []
    point_ids = {p["id"] for p in doc.get("points", [])}
    point_by_id = {p["id"]: p for p in doc.get("points", [])}

    # -- rule-engine v1: cap `when` at exactly one condition --------------
    if enforce_v1_policy_caps:
        for rule in doc.get("rules", []):
            if len(rule.get("when", [])) > 1:
                problems.append(
                    f"rules/{rule.get('id')}: rule-engine v1 allows exactly one "
                    f"condition in 'when'; found {len(rule['when'])}"
                )

    # -- analog v1: analog_in points are reserved, not runnable ------------
    if enforce_v1_policy_caps:
        for p in doc.get("points", []):
            if p.get("kind") == "analog_in":
                problems.append(
                    f"points/{p['id']}: kind 'analog_in' is reserved in v1 "
                    f"(analog scaling ships later); reject until the analog "
                    f"feature gate is enabled"
                )

    # -- dangling point references in rules --------------------------------
    for rule in doc.get("rules", []):
        for cond in rule.get("when", []):
            ref = cond.get("point")
            if ref not in point_ids:
                problems.append(f"rules/{rule['id']}/when: references unknown point '{ref}'")
        for action in rule.get("then", []) + rule.get("else", []):
            ref = action.get("point")
            if ref is not None and ref not in point_ids:
                problems.append(f"rules/{rule['id']}/then|else: references unknown point '{ref}'")

    # -- output contention: two enabled rules writing the same coil ------
    writers: dict[str, list[str]] = {}
    for rule in doc.get("rules", []):
        if not rule.get("enabled", True):
            continue
        for action in rule.get("then", []) + rule.get("else", []):
            if action.get("action") in ("set", "pulse"):
                writers.setdefault(action["point"], []).append(rule["id"])
    for point_id, rule_ids in writers.items():
        if len(rule_ids) > 1:
            problems.append(
                f"points/{point_id}: written by {len(rule_ids)} enabled rules "
                f"{rule_ids} — output contention"
            )

    # -- unit_id is the config-level device key and must be unique --------
    # NOTE: the execution plan's own io_config example shows two TCP devices
    # ("IO Module A" / "IO Module B") both carrying unit_id 1, distinguished
    # only by tcp.host. That's ambiguous: points[] resolves to a device via
    # unit_id alone, so two devices sharing one unit_id makes every point on
    # them unresolvable. Treating that as a documentation slip, not a real
    # requirement — unit_id must be unique per device regardless of
    # transport. The real Modbus wire identifier sent to a TCP module
    # (which is usually irrelevant once each module has its own IP) is a
    # separate, optional `tcp.slave_id` (default 1), not this field.
    unit_id_counts = Counter(d["unit_id"] for d in doc.get("devices", []))
    for unit_id, count in unit_id_counts.items():
        if count > 1:
            problems.append(
                f"devices: unit_id {unit_id} is used by {count} devices; "
                f"unit_id must be unique per device (see NOTE in validators.py)"
            )

    device_unit_ids = set(unit_id_counts)
    for p in doc.get("points", []):
        if p["unit_id"] not in device_unit_ids:
            problems.append(f"points/{p['id']}: unit_id {p['unit_id']} has no matching device")

    # -- TCP transport requires every device to carry tcp.host -------------
    if doc.get("bus", {}).get("transport") == "tcp":
        for d in doc.get("devices", []):
            if "tcp" not in d or not d["tcp"].get("host"):
                problems.append(f"devices/{d.get('name')}: transport is 'tcp' but device has no tcp.host")

    if problems:
        raise ConfigValidationError(problems)


def load_and_validate(path: str | Path, kind: str) -> dict:
    """Convenience: read a JSON file off disk and validate it by kind
    ('identity' | 'system' | 'io')."""
    doc = json.loads(Path(path).read_text())
    {
        "identity": validate_identity,
        "system": validate_system,
        "io": validate_io,
    }[kind](doc)
    return doc
