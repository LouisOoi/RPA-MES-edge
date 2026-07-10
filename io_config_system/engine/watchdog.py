"""
AR-02 — hardware watchdog interface.

The Architecture Review's finding: if the poll-engine process hangs
(deadlock, I/O stall, OOM) with no restart mechanism, whatever coil states
were last written just persist — an energized relay stays energized
indefinitely. That's fail-to-danger, not fail-safe (IEC 61508).

The real fix is a hardware watchdog timer that resets the board if nothing
pets it for N seconds — that's a board/BOM decision (see
IO_Config_Execution_Plan.md's AR-02 row and Architecture_Review.md), not
something pure software can invent on hardware that doesn't have one. What
this module DOES provide is the software side of that contract: a small,
swappable interface that `PollEngine.run_cycle()` calls every cycle, so
wiring in a real hardware watchdog later is a one-line constructor change,
not a PollEngine redesign.

`NullWatchdog` is the default and is exactly what its name says: a
no-op. Running with `NullWatchdog` is what AR-10 documents as the accepted
pilot-grade limitation — it is not a substitute for AR-02's hardware
requirement, and callers should not read "the interface exists" as "the
finding is closed."
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class Watchdog(Protocol):
    def pet(self) -> None:
        """Called once per poll cycle. Must be cheap and must never raise
        for a transient issue — a watchdog that itself destabilizes the
        poll loop defeats the point."""
        ...

    def close(self) -> None:
        """Release any underlying handle. Safe to call multiple times."""
        ...


class NullWatchdog:
    """No hardware watchdog present. This is the default so the engine
    runs unmodified on hardware that doesn't have one (or in tests) — but
    it means AR-02's actual protection (a board reset on process hang)
    does not exist while this is in use. See module docstring."""

    def pet(self) -> None:
        return None

    def close(self) -> None:
        return None


class LinuxHardwareWatchdog:
    """Pets a Linux hardware watchdog device (e.g. `/dev/watchdog`), the
    same interface most industrial Pi-class boards (RevPi, CM4 carriers
    with a watchdog IC) expose. Writing any byte to the device resets its
    countdown timer; if nothing writes to it before the timer expires, the
    kernel/board resets.

    Untestable in this sandbox — there is no real watchdog device here.
    Construction is deliberately lazy (the device is opened on first
    `pet()`, not in `__init__`) so unit tests can exercise the class's
    plumbing (device path handling, error wrapping) without a real device,
    while still failing loudly if `pet()` is ever actually called without
    one. This is intentional: silently falling back to a no-op here would
    quietly turn AR-02's hardware requirement back into AR-02's original
    gap.
    """

    def __init__(self, device_path: str | Path = "/dev/watchdog"):
        self.device_path = Path(device_path)
        self._fh = None

    def pet(self) -> None:
        if self._fh is None:
            try:
                self._fh = open(self.device_path, "wb", buffering=0)
            except OSError as exc:
                raise WatchdogUnavailable(
                    f"cannot open hardware watchdog device {self.device_path}: {exc}. "
                    f"Either provision real watchdog hardware, or use NullWatchdog "
                    f"explicitly and record that choice in the commissioning sheet "
                    f"(AR-10 accepted-limitation path) — do not let this fail silently."
                ) from exc
        self._fh.write(b"\x00")
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        if self._fh is not None:
            try:
                # Many watchdog drivers support a graceful-disable magic
                # close sequence; without it some boards reset on close.
                # Left unimplemented deliberately — see class docstring —
                # this is real hardware-specific behavior to confirm against
                # the actual board's datasheet, not something to guess at.
                self._fh.close()
            finally:
                self._fh = None


class WatchdogUnavailable(RuntimeError):
    """Raised by a real watchdog implementation when it cannot reach its
    hardware. Deliberately NOT caught anywhere in engine code — a poll
    engine that silently swallows "the safety device isn't there" is worse
    than one that fails loudly at startup."""
