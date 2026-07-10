"""
Runnable demo: stands up the multi-zone orchestrator + zone-scoped Flask
app (api/multi_zone_app.py) against SIMULATED Modbus hardware, so the
whole HTTP surface can be exercised without a real RTU/TCP device.

Not a test — this is meant to be run directly:

    python3 scripts/run_multi_zone_demo.py

It creates two zones ("weld_cell" on wired defaults, "leak_test_rig" on
wireless defaults) under a temp directory, starts both zones' poll loops
via ZoneOrchestrator, and serves the Flask app on 0.0.0.0:8080.

Default login: admin1 / admin-pass (admin tier), op1 / op-pass (operator
tier) — demo credentials only, never use these outside a throwaway demo.
"""
from __future__ import annotations

import copy
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import config_store  # noqa: E402
from engine.event_store import init_db  # noqa: E402
from engine.link_medium import recommended_comms_defaults  # noqa: E402
from engine.poll_engine import PollEngine  # noqa: E402
from engine.system_store import NullNetworkApplier  # noqa: E402
from engine.zone_orchestrator import ZoneOrchestrator  # noqa: E402

from api.auth import UserStore  # noqa: E402
from api.multi_zone_app import ZoneResources, create_multi_zone_app  # noqa: E402

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"


class SimulatedModbusClient:
    """A tiny in-memory Modbus stand-in, shaped like pymodbus's clients
    (read_coils/write_coil/etc, .isError()/.bits/.registers) — the same
    contract tests/fake_modbus_client.py uses, reimplemented here so this
    script has zero dependency on the tests/ package. Coils/registers
    start at a fixed, slightly-alive-looking state so `/live` shows
    something other than all-zero on first load."""

    def __init__(self) -> None:
        self.coils: dict[tuple[int, int], bool] = {}
        self.registers: dict[tuple[int, int], int] = {}
        self.calls: list[tuple] = []

    def connect(self) -> bool:
        return True

    def close(self) -> None:
        pass

    class _Resp:
        def __init__(self, *, bits=None, registers=None):
            self._bits, self._registers = bits, registers

        def isError(self) -> bool:
            return False

        @property
        def bits(self):
            return self._bits

        @property
        def registers(self):
            return self._registers

    def read_coils(self, address, *, count=1, device_id=1):
        self.calls.append(("read_coils", address, count, device_id))
        bits = [self.coils.get((device_id, address + i), False) for i in range(count)]
        return self._Resp(bits=bits)

    def read_holding_registers(self, address, *, count=1, device_id=1):
        self.calls.append(("read_holding_registers", address, count, device_id))
        regs = [self.registers.get((device_id, address + i), 0) for i in range(count)]
        return self._Resp(registers=regs)

    def read_input_registers(self, address, *, count=1, device_id=1):
        self.calls.append(("read_input_registers", address, count, device_id))
        regs = [self.registers.get((device_id, address + i), 0) for i in range(count)]
        return self._Resp(registers=regs)

    def write_coil(self, address, value, *, device_id=1):
        self.calls.append(("write_coil", address, value, device_id))
        self.coils[(device_id, address)] = value
        return self._Resp()


def _clients_factory(io_config):
    shared = SimulatedModbusClient()
    return {d["unit_id"]: shared for d in io_config["devices"]}


def _write_zone(base_dir: Path, zone_id: str, *, medium: str) -> Path:
    zone_dir = base_dir / zone_id
    zone_dir.mkdir(parents=True)

    io_config = json_load(SEED_DIR / "io_config.seed.v2.golden.json")
    io_config = copy.deepcopy(io_config)
    io_config["link"] = {"medium": medium}
    defaults = recommended_comms_defaults(medium)
    io_config["bus"]["poll_interval_ms"] = defaults["poll_interval_ms"]
    if io_config["bus"]["transport"] == "rtu":
        io_config["bus"]["serial"]["timeout_ms"] = defaults["timeout_ms"]
        io_config["bus"]["serial"]["retries"] = defaults["retries"]
        io_config["bus"]["serial"]["backoff_ms"] = defaults["backoff_ms"]

    identity = json_load(SEED_DIR / "ctrl_id.seed.json")
    identity = copy.deepcopy(identity)
    identity["zone_id"] = zone_id
    system_config = json_load(SEED_DIR / "system_config.seed.json")

    config_store.atomic_write_json(zone_dir / "io_config.json", io_config)
    config_store.atomic_write_json(zone_dir / "ctrl_id.json", identity)
    config_store.atomic_write_json(zone_dir / "system_config.json", system_config)
    init_db(zone_dir / "event_log.db")
    return zone_dir


def json_load(path: Path) -> dict:
    import json
    return json.loads(path.read_text())


def build_demo_app(base_dir: Path):
    orchestrator = ZoneOrchestrator()
    zone_resources = {}

    for zone_id, medium in (("weld_cell", "wired"), ("leak_test_rig", "wireless")):
        zone_dir = _write_zone(base_dir, zone_id, medium=medium)
        io_config = config_store.read_json(zone_dir / "io_config.json")
        identity = config_store.read_json(zone_dir / "ctrl_id.json")
        db_path = zone_dir / "event_log.db"

        engine = PollEngine(
            io_config, identity, db_path,
            clients=_clients_factory(io_config), clients_factory=_clients_factory,
            config_path=zone_dir / "io_config.json",
        )
        orchestrator.add_zone(zone_id, engine)
        zone_resources[zone_id] = ZoneResources(
            identity_path=zone_dir / "ctrl_id.json", system_path=zone_dir / "system_config.json",
            io_config_path=zone_dir / "io_config.json", network_applier=NullNetworkApplier(),
        )

    users = UserStore()
    users.add_user("admin1", "admin-pass", "admin")
    users.add_user("op1", "op-pass", "operator")

    app = create_multi_zone_app(
        orchestrator=orchestrator, zone_resources=zone_resources,
        user_store=users, secret_key="demo-only-not-for-production",
    )
    return app, orchestrator


if __name__ == "__main__":
    demo_dir = Path(tempfile.mkdtemp(prefix="rpa_mes_multizone_demo_"))
    print(f"[demo] zone data directory: {demo_dir}")
    app, orchestrator = build_demo_app(demo_dir)
    orchestrator.start_all()
    print(f"[demo] zones running: {orchestrator.zone_ids()}")
    print("[demo] login: admin1/admin-pass (admin), op1/op-pass (operator)")
    print("[demo] try: curl http://127.0.0.1:8080/api/status")
    try:
        app.run(host="0.0.0.0", port=8080, threaded=True)
    finally:
        orchestrator.stop_all()
        shutil.rmtree(demo_dir, ignore_errors=True)
