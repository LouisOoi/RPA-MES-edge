"""
On-disk config persistence for Phase 4 hot reload.

Two guarantees this module exists to provide:
  1. Atomic writes. A crash mid-save must never leave a half-written JSON
     file where io_config.json used to be — write to a temp file in the
     same directory, then os.replace() (atomic rename on POSIX) over the
     real path.
  2. Change detection by `config_version`, not mtime. mtime can be wrong
     across filesystems, clock skew, or a restore from backup; the config
     itself carries a monotonic counter that's the actual source of truth
     for "has this changed since I last looked."
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, doc: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(json.dumps(doc, indent=2))
    os.replace(tmp_path, path)  # atomic on POSIX: never a partial `path`


def read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def lkg_path(path: str | Path) -> Path:
    path = Path(path)
    return path.with_name(f"{path.name}.lkg")


def backup_as_lkg(path: str | Path, doc: dict) -> None:
    """Write `doc` (expected to be the config that was actually running
    immediately before a reload) to the LKG sibling file. Takes the doc
    directly rather than re-reading `path`, because by the time this is
    called `path` may already hold the NEW config (e.g. an external writer
    already replaced it before the engine noticed and reloaded)."""
    atomic_write_json(lkg_path(path), doc)


class ConfigWatcher:
    """Polls a config file for a `config_version` change. Never raises on
    a missing file or a transient partial read (a concurrent atomic_write_json
    can never leave a half file, but a watcher polling at the wrong
    nanosecond could still see the file mid-rename on some filesystems) —
    either case is treated as "nothing new yet," picked up on the next
    poll() once the write settles.
    """

    def __init__(self, path: str | Path, *, initial_version: Any = None) -> None:
        self.path = Path(path)
        self._last_seen_version = initial_version

    def poll(self) -> dict | None:
        """Returns the parsed doc if config_version differs from what was
        last seen, else None. Does not update `_last_seen_version` on a
        failed read, only on a successful one that differs."""
        try:
            doc = read_json(self.path)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        version = doc.get("config_version")
        if version != self._last_seen_version:
            self._last_seen_version = version
            return doc
        return None

    def mark_seen(self, version: Any) -> None:
        """Used at startup so the version the engine was already
        constructed with isn't reported as a 'change' on the first poll."""
        self._last_seen_version = version
