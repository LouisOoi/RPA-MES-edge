"""
Windows Service host for the multi-zone orchestrator (Deployment Variant
B). See IO_Config_Execution_Plan.md's multi-zone orchestrator item 2:
"a Windows Service wrapper with its own watchdog/restart logic in place
of systemd."

Honest limits, same posture as engine/watchdog.py and
engine/key_storage.py: there is no Windows machine in this sandbox to run
or test a real Windows Service against — pywin32's `win32serviceutil`
only functions on Windows, full stop. This module is written so that
IMPORTING it never fails on Linux/macOS (the `pywin32` import is
deferred into the class body / guarded), but actually running it as a
service is untested and untestable here. What's real and tested on any
OS: `ZoneOrchestrator` (engine/zone_orchestrator.py) and
`load_all_zones`/`load_zone_from_directory` (engine/zone_loader.py) —
this file is a thin, OS-specific shell around them. Swapping in a real
Flask+waitress HTTP server call and wiring this into `sc create`/
`win32serviceutil.InstallService` on an actual Windows box is a small,
isolated next step BECAUSE the orchestrator/loader underneath it already
works and is already tested.

Usage on a real Windows box (not exercised here):
    python windows_service.py install
    python windows_service.py start
    python windows_service.py stop
    python windows_service.py remove

Configuration (zones root directory, Flask host/port, secret key, user
accounts) is intentionally NOT hardcoded here — see `_load_config()`,
which reads a `service_config.json` next to this file. That file is not
part of `io_config_system`'s schema-validated config surface; it's
host/deployment configuration for the service process itself.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from engine.zone_loader import load_all_zones
from engine.zone_orchestrator import ZoneOrchestrator

SERVICE_NAME = "RpaMesEdgeMultiZone"
SERVICE_DISPLAY_NAME = "RPA-MES-edge Multi-Zone Field IO Terminal"


def _load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} not found — a Windows Service deployment needs a "
            f"service_config.json next to this file with at least {{'zones_root': '...'}}"
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


def build_orchestrator(zones_root: str | Path) -> ZoneOrchestrator:
    """The OS-agnostic core of this whole module: load every zone under
    `zones_root` and hand back a ready-to-start ZoneOrchestrator. Calling
    THIS function (not the service class below) is how tests exercise
    everything real about "hosting multiple zones" without needing
    Windows or pywin32 at all."""
    orchestrator = ZoneOrchestrator()
    zones = load_all_zones(zones_root)
    for zone_id, (engine, _resource_paths) in zones.items():
        orchestrator.add_zone(zone_id, engine)
    return orchestrator


def _run_foreground(zones_root: str | Path) -> None:
    """What the Windows Service's SvcDoRun ultimately calls — pulled out
    as a plain function so it can also be run directly (`python
    windows_service.py run`) for local testing on a real Windows box
    without installing it as a service first."""
    orchestrator = build_orchestrator(zones_root)
    orchestrator.start_all()
    return orchestrator


try:
    import win32event
    import win32service
    import win32serviceutil

    class MultiZoneWindowsService(win32serviceutil.ServiceFramework):
        """The actual Windows Service class. Untested in this
        environment — see module docstring. `SvcDoRun`/`SvcStop` are the
        two methods Windows itself calls; everything they do delegates
        immediately to the OS-agnostic functions above."""

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.orchestrator: ZoneOrchestrator | None = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.orchestrator is not None:
                self.orchestrator.stop_all()
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            config = _load_config(Path(__file__).parent / "service_config.json")
            self.orchestrator = build_orchestrator(config["zones_root"])
            self.orchestrator.start_all()
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

except ImportError:
    # pywin32 isn't installed (expected on any non-Windows box, including
    # this dev sandbox). Importing this module still succeeds; only
    # actually running it as a Windows Service requires pywin32 + Windows.
    MultiZoneWindowsService = None  # type: ignore[assignment]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        # Local foreground run for testing on a real box — no service
        # install required. Ctrl+C to stop.
        import time

        config = _load_config(Path(__file__).parent / "service_config.json")
        orch = _run_foreground(config["zones_root"])
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            orch.stop_all()
    elif MultiZoneWindowsService is not None:
        win32serviceutil.HandleCommandLine(MultiZoneWindowsService)
    else:
        print("pywin32 is required to install/start/stop this as a Windows Service. "
              "Run with 'run' for a local foreground test instead.", file=sys.stderr)
        sys.exit(1)
