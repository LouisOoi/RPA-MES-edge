"""
Zone-scoped Flask routes for Deployment Variant B (multi-zone). See
IO_Config_Execution_Plan.md's multi-zone orchestrator item 3: "one
terminal's web UI can commission and monitor each zone independently
rather than assuming a single global io_config" — this is ONE Flask app
process (not one per zone, unlike Variant A's per-terminal app), serving
every zone via `/api/zone/<zone_id>/...` routes. The engine layer
underneath stays exactly Variant A: each zone is its own independent
PollEngine, supervised by a ZoneOrchestrator (engine/zone_orchestrator.py)
— this module is purely the HTTP surface over that.

Deliberately NOT a rewrite of api/app.py: the single-terminal app stays
untouched (Variant A is "built, tested, and unchanged by what follows",
per the plan) and continues to be the right choice for a one-zone
deployment. This module is additive, for the N-zone case only.

Login/session/auth (api/auth.py's UserStore, tier gating, AR-05 lockout)
is shared across all zones on one terminal — a single operator/admin
login governs every zone this Flask process serves, matching "one
terminal's web UI."
"""
from __future__ import annotations

import time
from functools import wraps

from flask import Flask, jsonify, request, session

from engine import bus_scan as bus_scan_mod
from engine import config_store, identity_store, io_export, system_store
from engine.event_store import log_event
from engine.zone_orchestrator import ZoneOrchestrator

from .auth import TIER_LEVEL, AccountLocked


def _error(status: int, error_code: str, problems: list[str] | None = None, extra: dict | None = None):
    body = {"error": error_code}
    if problems:
        body["problems"] = problems
    if extra:
        body.update(extra)
    return jsonify(body), status


class ZoneResources:
    """Per-zone file paths + collaborators the API layer needs on top of
    the PollEngine the orchestrator already owns. One of these per zone,
    keyed by zone_id — the multi-zone equivalent of the individual path
    arguments api/app.py's create_app() takes for a single terminal."""

    def __init__(
        self, *, identity_path, system_path, io_config_path, network_applier,
    ) -> None:
        self.identity_path = identity_path
        self.system_path = system_path
        self.io_config_path = io_config_path
        self.network_applier = network_applier


def create_multi_zone_app(
    *,
    orchestrator: ZoneOrchestrator,
    zone_resources: dict[str, ZoneResources],
    user_store,
    secret_key: str = "dev-only-change-me",
    require_tls: bool = False,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secret_key

    if require_tls:
        # AR-05, same contract as api/app.py's single-terminal version —
        # see that module for the full reasoning.
        @app.before_request
        def _reject_insecure_transport():
            if request.path == "/api/status":
                return None
            is_secure = request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
            if not is_secure:
                return _error(403, "tls_required")

    def require_tier(min_tier: str):
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                tier = session.get("tier")
                if tier is None:
                    return _error(401, "unauthorized")
                if TIER_LEVEL[tier] < TIER_LEVEL[min_tier]:
                    return _error(403, "forbidden")
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    def _zone_or_404(zone_id: str):
        """Every zone-scoped route funnels through here first — an
        unknown zone_id is a 404, not a 422/500, since it's a URL
        addressing problem, not a payload problem."""
        if zone_id not in zone_resources:
            return None, None
        return orchestrator.get_engine(zone_id), zone_resources[zone_id]

    # -- shared, not zone-scoped ------------------------------------------

    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        username, password = data.get("username"), data.get("password")
        if not username or not password:
            return _error(401, "unauthorized")
        try:
            tier = user_store.verify(username, password)
        except AccountLocked as exc:
            return _error(423, "account_locked", [
                f"account locked after repeated failed logins; retry in {round(exc.retry_after_s)}s",
            ])
        if tier is None:
            return _error(401, "unauthorized")
        session["tier"] = tier
        session["username"] = username
        return jsonify({"tier": tier, "username": username})

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/status")
    def status():
        return jsonify({"ok": True})

    @app.get("/api/zones")
    @require_tier("operator")
    def list_zones():
        """Fleet-view convenience endpoint — not called for in the plan
        by name, but a direct consequence of "one terminal's web UI...
        monitor each zone independently": something has to enumerate
        what zones exist and whether each one's supervised thread is
        actually running."""
        return jsonify({"zones": [
            {
                "zone_id": zone_id,
                "running": orchestrator.is_running(zone_id),
                "crash_count": orchestrator.crash_count(zone_id),
                "last_error": orchestrator.last_error(zone_id),
                "config_version": orchestrator.get_engine(zone_id).io_config.get("config_version"),
            }
            for zone_id in orchestrator.zone_ids()
        ]})

    # -- zone-scoped, admin tier -------------------------------------------

    @app.get("/api/zone/<zone_id>/identity")
    @require_tier("admin")
    def get_identity(zone_id):
        _, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        return jsonify({"identity": config_store.read_json(res.identity_path), "boot_id_editable": False})

    @app.put("/api/zone/<zone_id>/identity")
    @require_tier("admin")
    def put_identity(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        data = request.get_json(silent=True) or {}
        confirm = bool(data.pop("confirm_breaks_continuity", False))
        try:
            new_identity = identity_store.update_identity(
                res.identity_path, engine.db_path, data,
                updated_by=session["username"], confirm=confirm,
            )
        except identity_store.IdentityUpdateError as exc:
            return _error(422, "validation_failed", exc.problems)
        return jsonify({"identity": new_identity, "boot_id_editable": False})

    @app.get("/api/zone/<zone_id>/system")
    @require_tier("admin")
    def get_system(zone_id):
        _, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        return jsonify(system_store.load_system_config(res.system_path))

    @app.put("/api/zone/<zone_id>/system")
    @require_tier("admin")
    def put_system(zone_id):
        _, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        data = request.get_json(silent=True) or {}
        if request.args.get("test_only") == "true":
            mqtt = data.get("mqtt", {})
            result = system_store.check_mqtt_connection(mqtt.get("broker_host", ""), mqtt.get("port", 0))
            return jsonify({"ok": result.ok, "message": result.message})
        try:
            result = system_store.save_system_config(res.system_path, data, res.network_applier)
        except system_store.SystemUpdateError as exc:
            return _error(422, "validation_failed", exc.problems)
        if not result.ok:
            return _error(409, "conflict", [result.message])
        return jsonify({"ok": True, "message": result.message, "warnings": result.warnings})

    @app.post("/api/zone/<zone_id>/commissioning-mode")
    @require_tier("admin")
    def set_commissioning_mode(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        data = request.get_json(silent=True) or {}
        engine.test_write_manager.set_commissioning_mode(bool(data.get("enabled", False)))
        return jsonify({"commissioning_mode": engine.test_write_manager.commissioning_mode})

    # -- zone-scoped, operator tier -----------------------------------------

    def _persist_io_config(zone_id, engine, res):
        if res.io_config_path is not None:
            config_store.atomic_write_json(res.io_config_path, engine.io_config)

    @app.get("/api/zone/<zone_id>/io")
    @require_tier("operator")
    def get_io(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        return jsonify(engine.io_config)

    @app.put("/api/zone/<zone_id>/io")
    @require_tier("operator")
    def put_io(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        data = request.get_json(silent=True) or {}
        permit_acknowledged = bool(data.pop("permit_acknowledged", False))
        new_config = {
            **data,
            "config_version": engine.io_config["config_version"] + 1,
            "updated_at": int(time.time() * 1000),
            "updated_by": session["username"],
        }
        result = engine.reload(new_config, permit_acknowledged=permit_acknowledged)
        if not result.ok:
            if result.requires_permit:
                return _error(409, "permit_required", result.problems,
                              extra={"pending_output_states": result.pending_output_states})
            return _error(422, "validation_failed", result.problems)
        _persist_io_config(zone_id, engine, res)
        return jsonify({"config_version": new_config["config_version"]})

    @app.get("/api/zone/<zone_id>/live")
    @require_tier("operator")
    def get_live(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        return jsonify({"points": engine.snapshot.as_dict()})

    @app.post("/api/zone/<zone_id>/bus/scan")
    @require_tier("operator")
    def bus_scan(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        data = request.get_json(silent=True) or {}
        transport = data.get("transport")
        if transport == "rtu":
            client = next(iter(engine.clients.values()))
            hits = bus_scan_mod.scan_rtu(client)
        elif transport == "tcp":
            hits = bus_scan_mod.scan_tcp(data.get("ip_range", ""))
        else:
            return _error(422, "validation_failed", ["transport must be 'rtu' or 'tcp'"])
        return jsonify({"found": [
            {"unit_id": h.unit_id, "host": h.host, "responded_ms": h.responded_ms} for h in hits
        ]})

    @app.post("/api/zone/<zone_id>/test/write")
    @require_tier("operator")
    def test_write(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        data = request.get_json(silent=True) or {}
        result = engine.request_test_write(
            data.get("point"), data.get("value"), confirm=bool(data.get("confirm", False)),
            timeout_ms=int(data.get("timeout_ms", 5000)),
        )
        if not result.ok:
            return _error(409, "conflict", [result.message])
        return jsonify({"ok": True, "message": result.message})

    @app.get("/api/zone/<zone_id>/config/versions")
    @require_tier("operator")
    def config_versions(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        return jsonify({"versions": engine.list_config_versions()})

    @app.post("/api/zone/<zone_id>/config/rollback")
    @require_tier("operator")
    def config_rollback(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        data = request.get_json(silent=True) or {}
        version = data.get("version")
        result = engine.rollback_to_version(version) if version is not None else engine.rollback_to_lkg()
        if not result.ok:
            return _error(422, "validation_failed", result.problems)
        _persist_io_config(zone_id, engine, res)
        return jsonify({"config_version": engine.io_config.get("config_version")})

    @app.get("/api/zone/<zone_id>/io/export")
    @require_tier("operator")
    def io_export_endpoint(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        return jsonify(io_export.export_io_config(engine.io_config))

    @app.post("/api/zone/<zone_id>/io/import")
    @require_tier("operator")
    def io_import_endpoint(zone_id):
        engine, res = _zone_or_404(zone_id)
        if res is None:
            return _error(404, "zone_not_found")
        data = request.get_json(silent=True) or {}
        permit_acknowledged = bool(data.pop("permit_acknowledged", False))
        try:
            new_config = io_export.build_import_doc(
                data, current_config_version=engine.io_config["config_version"],
                updated_by=session["username"],
            )
        except ValueError as exc:
            return _error(422, "validation_failed", [str(exc)])
        result = engine.reload(new_config, permit_acknowledged=permit_acknowledged)
        if not result.ok:
            if result.requires_permit:
                return _error(409, "permit_required", result.problems,
                              extra={"pending_output_states": result.pending_output_states})
            return _error(422, "validation_failed", result.problems)
        _persist_io_config(zone_id, engine, res)
        return jsonify({"config_version": new_config["config_version"]})

    return app
