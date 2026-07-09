"""
Minimal Flask app implementing the admin-tier slice of api_contract.md
needed for Phase 5: login, identity, system (network/mqtt/time) config +
MQTT test-connection. This is the first real HTTP layer in the project —
Phases 0-4 were pure engine code exercised directly in tests. An automated
test can't literally drive a browser, so Flask's test client stands in for
"through the browser, no SSH": it exercises the exact same WSGI request/
response path a real browser would, just without rendering HTML.

Endpoints beyond this slice (operator-tier /api/io, /api/live, bus scan,
test write, factory reset, OTA) are documented in api_contract.md but
belong to later phases (6/7) and aren't implemented here.
"""
from __future__ import annotations

from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, session

from engine import config_store, identity_store, system_store

from .auth import TIER_LEVEL


def _error(status: int, error_code: str, problems: list[str] | None = None):
    body = {"error": error_code}
    if problems:
        body["problems"] = problems
    return jsonify(body), status


def create_app(
    *,
    identity_path: str | Path,
    system_path: str | Path,
    db_path: str | Path,
    user_store,
    network_applier,
    secret_key: str = "dev-only-change-me",
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secret_key

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

    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        username, password = data.get("username"), data.get("password")
        tier = user_store.verify(username, password) if username and password else None
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
        # No auth required — health check only, no config data (per contract).
        return jsonify({"ok": True})

    @app.get("/api/identity")
    @require_tier("admin")
    def get_identity():
        ident = config_store.read_json(identity_path)
        return jsonify({"identity": ident, "boot_id_editable": False})

    @app.put("/api/identity")
    @require_tier("admin")
    def put_identity():
        data = request.get_json(silent=True) or {}
        confirm = bool(data.pop("confirm_breaks_continuity", False))
        try:
            new_identity = identity_store.update_identity(
                identity_path, db_path, data,
                updated_by=session["username"], confirm=confirm,
            )
        except identity_store.IdentityUpdateError as exc:
            return _error(422, "validation_failed", exc.problems)
        return jsonify({"identity": new_identity, "boot_id_editable": False})

    @app.get("/api/system")
    @require_tier("admin")
    def get_system():
        return jsonify(system_store.load_system_config(system_path))

    @app.put("/api/system")
    @require_tier("admin")
    def put_system():
        data = request.get_json(silent=True) or {}
        if request.args.get("test_only") == "true":
            mqtt = data.get("mqtt", {})
            result = system_store.check_mqtt_connection(mqtt.get("broker_host", ""), mqtt.get("port", 0))
            return jsonify({"ok": result.ok, "message": result.message})

        try:
            result = system_store.save_system_config(system_path, data, network_applier)
        except system_store.SystemUpdateError as exc:
            return _error(422, "validation_failed", exc.problems)
        if not result.ok:
            return _error(409, "conflict", [result.message])
        return jsonify({"ok": True, "message": result.message})

    return app
