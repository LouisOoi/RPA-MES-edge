"""
Minimal Flask app implementing api_contract.md: Phase 5's admin-tier slice
(login, identity, system config + MQTT test-connection) plus Phase 6's
operator-tier commissioning/safety tools (io read/write, live values, bus
scan, test write, version history + rollback, export/import). This is the
first real HTTP layer in the project — Phases 0-4 were pure engine code
exercised directly in tests. An automated test can't literally drive a
browser, so Flask's test client stands in for "through the browser, no
SSH": it exercises the exact same WSGI request/response path a real
browser would, just without rendering HTML.

Plus Phase 7's admin-tier OTA routes, registered only when `ota_public_key`
is given — the signing key is a server-side trust anchor injected by
whoever deploys this app, never something a client supplies.

The Phase 6/7 routes are only registered when `poll_engine` (Phase 6) /
`ota_public_key` (Phase 7) are given to create_app() — a Phase 5-style
deployment (or Phase 5's own tests) that doesn't pass them simply doesn't
get those routes at all, rather than getting them wired to a None and
failing at request time.

Endpoints still not implemented: factory-reset.
"""
from __future__ import annotations

import base64
import time
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, session

from engine import bus_scan as bus_scan_mod
from engine import config_store, identity_store, io_export, ota, ota_state, system_store
from engine.event_store import log_event

from .auth import TIER_LEVEL, AccountLocked


def _error(status: int, error_code: str, problems: list[str] | None = None, extra: dict | None = None):
    body = {"error": error_code}
    if problems:
        body["problems"] = problems
    if extra:
        body.update(extra)
    return jsonify(body), status


def create_app(
    *,
    identity_path: str | Path,
    system_path: str | Path,
    db_path: str | Path,
    user_store,
    network_applier,
    secret_key: str = "dev-only-change-me",
    poll_engine=None,
    io_config_path: str | Path | None = None,
    ota_public_key=None,
    ota_status_path: str | Path | None = None,
    require_tls: bool = False,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secret_key

    if require_tls:
        # AR-05: "Config UI served over TLS." This process itself doesn't
        # terminate TLS (see docs/INSTALLER_GUIDE.md — that's a reverse
        # proxy's job in every deployment shape this product has), so what
        # this flag actually enforces is that the request arrived over a
        # secure channel by the time it reaches Flask: either Flask itself
        # sees a real TLS socket (request.is_secure), or a trusted reverse
        # proxy in front of it says so via X-Forwarded-Proto. Set this flag
        # only when such a proxy is actually in place — otherwise the
        # header is just a client-supplied lie and this becomes a no-op
        # that looks like a control.
        @app.before_request
        def _reject_insecure_transport():
            if request.path == "/api/status":
                return None  # health check stays reachable for LB probes
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

    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        username, password = data.get("username"), data.get("password")

        if not username or not password:
            return _error(401, "unauthorized")

        try:
            tier = user_store.verify(username, password)
        except AccountLocked as exc:
            # AR-05: logged with the typed username itself (not a session
            # identity — there isn't one yet, login just failed) purely as
            # a security-monitoring record, never used to authorize
            # anything downstream.
            ident = config_store.read_json(identity_path)
            log_event(db_path, ident, "auth_account_locked", {
                "username": username, "retry_after_s": round(exc.retry_after_s),
            })
            resp = _error(423, "account_locked", [
                f"account locked after repeated failed logins; retry in {round(exc.retry_after_s)}s",
            ])
            return resp

        if tier is None:
            ident = config_store.read_json(identity_path)
            log_event(db_path, ident, "auth_login_failed", {"username": username})
            return _error(401, "unauthorized")

        session["tier"] = tier
        session["username"] = username
        ident = config_store.read_json(identity_path)
        log_event(db_path, ident, "auth_login_success", {"username": username, "tier": tier})
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
        return jsonify({"ok": True, "message": result.message, "warnings": result.warnings})

    if poll_engine is not None:
        def _persist_io_config():
            if io_config_path is not None:
                config_store.atomic_write_json(io_config_path, poll_engine.io_config)

        @app.get("/api/io")
        @require_tier("operator")
        def get_io():
            return jsonify(poll_engine.io_config)

        @app.put("/api/io")
        @require_tier("operator")
        def put_io():
            data = request.get_json(silent=True) or {}
            # AR-07: not part of the persisted config — pulled off the
            # request separately so a permit acknowledgement can never be
            # smuggled in via the config body itself.
            permit_acknowledged = bool(data.pop("permit_acknowledged", False))
            new_config = {
                **data,
                "config_version": poll_engine.io_config["config_version"] + 1,
                "updated_at": int(time.time() * 1000),
                "updated_by": session["username"],
            }
            result = poll_engine.reload(new_config, permit_acknowledged=permit_acknowledged)
            if not result.ok:
                if result.requires_permit:
                    return _error(409, "permit_required", result.problems,
                                  extra={"pending_output_states": result.pending_output_states})
                return _error(422, "validation_failed", result.problems)
            _persist_io_config()
            return jsonify({"config_version": new_config["config_version"]})

        @app.get("/api/live")
        @require_tier("operator")
        def get_live():
            return jsonify({"points": poll_engine.snapshot.as_dict()})

        @app.post("/api/bus/scan")
        @require_tier("operator")
        def bus_scan():
            data = request.get_json(silent=True) or {}
            transport = data.get("transport")
            if transport == "rtu":
                client = next(iter(poll_engine.clients.values()))
                hits = bus_scan_mod.scan_rtu(client)
            elif transport == "tcp":
                hits = bus_scan_mod.scan_tcp(data.get("ip_range", ""))
            else:
                return _error(422, "validation_failed", ["transport must be 'rtu' or 'tcp'"])
            return jsonify({"found": [
                {"unit_id": h.unit_id, "host": h.host, "responded_ms": h.responded_ms} for h in hits
            ]})

        @app.post("/api/test/write")
        @require_tier("operator")
        def test_write():
            data = request.get_json(silent=True) or {}
            result = poll_engine.request_test_write(
                data.get("point"), data.get("value"), confirm=bool(data.get("confirm", False)),
                timeout_ms=int(data.get("timeout_ms", 5000)),
            )
            if not result.ok:
                return _error(409, "conflict", [result.message])
            return jsonify({"ok": True, "message": result.message})

        @app.post("/api/commissioning-mode")
        @require_tier("admin")  # gating the MODE toggle at admin tier, per the plan
        def set_commissioning_mode():
            data = request.get_json(silent=True) or {}
            poll_engine.test_write_manager.set_commissioning_mode(bool(data.get("enabled", False)))
            return jsonify({"commissioning_mode": poll_engine.test_write_manager.commissioning_mode})

        @app.get("/api/config/versions")
        @require_tier("operator")
        def config_versions():
            return jsonify({"versions": poll_engine.list_config_versions()})

        @app.post("/api/config/rollback")
        @require_tier("operator")
        def config_rollback():
            data = request.get_json(silent=True) or {}
            version = data.get("version")
            result = poll_engine.rollback_to_version(version) if version is not None else poll_engine.rollback_to_lkg()
            if not result.ok:
                return _error(422, "validation_failed", result.problems)
            _persist_io_config()
            return jsonify({"config_version": poll_engine.io_config.get("config_version")})

        @app.get("/api/io/export")
        @require_tier("operator")
        def io_export_endpoint():
            return jsonify(io_export.export_io_config(poll_engine.io_config))

        @app.post("/api/io/import")
        @require_tier("operator")
        def io_import_endpoint():
            data = request.get_json(silent=True) or {}
            permit_acknowledged = bool(data.pop("permit_acknowledged", False))
            try:
                new_config = io_export.build_import_doc(
                    data, current_config_version=poll_engine.io_config["config_version"],
                    updated_by=session["username"],
                )
            except ValueError as exc:
                return _error(422, "validation_failed", [str(exc)])
            result = poll_engine.reload(new_config, permit_acknowledged=permit_acknowledged)
            if not result.ok:
                if result.requires_permit:
                    return _error(409, "permit_required", result.problems,
                                  extra={"pending_output_states": result.pending_output_states})
                return _error(422, "validation_failed", result.problems)
            _persist_io_config()
            return jsonify({"config_version": new_config["config_version"]})

        if ota_public_key is not None:
            @app.post("/api/ota/apply")
            @require_tier("admin")
            def ota_apply():
                data = request.get_json(silent=True) or {}
                manifest = data.get("manifest")
                signature_b64 = data.get("signature")
                if not manifest or not signature_b64:
                    return _error(422, "validation_failed", ["manifest and signature are required"])
                try:
                    signature = base64.b64decode(signature_b64)
                except (ValueError, TypeError):
                    return _error(422, "validation_failed", ["signature is not valid base64"])

                migrate_result = ota.verify_and_migrate(
                    poll_engine.io_config, manifest, signature, public_key=ota_public_key,
                )
                if not migrate_result.ok:
                    return _error(422, "validation_failed", migrate_result.problems)

                result = ota.apply_and_reload(
                    poll_engine, migrate_result.migrated_config,
                    io_config_path=io_config_path, ident=poll_engine.ident, db_path=poll_engine.db_path,
                    status_path=ota_status_path,
                )
                if not result.ok:
                    status = 409 if result.rolled_back else 422
                    return _error(status, "conflict" if result.rolled_back else "validation_failed", result.problems)
                return jsonify({"ok": True, "config_version": result.config_version})

            @app.get("/api/ota/status")
            @require_tier("admin")
            def ota_status():
                status = ota_state.read_ota_status(ota_status_path) if ota_status_path else None
                return jsonify(status or {"ok": None, "message": "no OTA has been applied yet"})

    return app
