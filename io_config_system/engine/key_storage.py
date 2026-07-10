"""
AR-06 (Architecture Review) — MQTT client private key storage.

Honest limits, same posture as engine/watchdog.py: the finding is "TLS
private keys sit in clear on removable media" and the fix the plan
prescribes is secure-element/TPM-backed storage. There is no secure
element or TPM in this dev sandbox to provision or test a real
implementation against, and faking one that's never touched real hardware
would be worse than being explicit that it doesn't exist yet. What this
module provides is the interface a real implementation must satisfy
(`KeyStore`), the honest default that describes what every deployment of
this codebase actually does today (`FileKeyStore` — a plain file, still
extractable), and a scaffold for the real fix (`Pkcs11KeyStore`) that
fails loudly rather than silently degrading if it's asked to produce a key
reference without a real module configured underneath it.

Swapping a real `Pkcs11KeyStore` (or an equivalent TPM-backed
implementation) in on-device is a small, isolated change BECAUSE this
interface exists — same argument as the watchdog scaffold.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class KeyStorageUnavailable(RuntimeError):
    """Raised when a hardware-backed key store is configured but the
    hardware/module it depends on isn't actually present. Fails loudly —
    never silently falls back to an extractable file, because that
    fallback is exactly the vulnerability AR-06 exists to close."""


class KeyStore(Protocol):
    def get_client_key_reference(self) -> str:
        """Returns an opaque reference to the MQTT client private key,
        usable by whatever TLS layer the MQTT client uses to authenticate.
        For the insecure default (`FileKeyStore`) this is a plain
        filesystem path. For a real hardware-backed implementation this
        must be a reference (e.g. a PKCS#11 URI) that the TLS stack's own
        PKCS#11 engine can dereference — the raw key material must never
        pass through this process, not even transiently."""
        ...

    def is_extractable(self) -> bool:
        """True if the key this refers to is sitting in a form anyone
        with filesystem access to the device could copy off it whole.
        AR-06 exists specifically to drive this to False on real
        deployments. Every implementation that exists in this codebase
        today returns True, honestly, until real secure-element hardware
        is available to build and test against."""
        ...


class FileKeyStore:
    """The only implementation that exists today, and the one every
    deployment of this codebase currently uses without knowing it: the
    key is exactly the plain PEM/DER file `system.mqtt.client_key` has
    always pointed at. Kept as an explicit, honestly-labeled class
    (rather than just "no KeyStore at all") so `is_extractable()` gives
    callers something to check and warn on — see
    system_store.flag_extractable_client_key()."""

    def __init__(self, key_path: str | Path) -> None:
        self._key_path = str(key_path)

    def get_client_key_reference(self) -> str:
        return self._key_path

    def is_extractable(self) -> bool:
        return True


class Pkcs11KeyStore:
    """Scaffold for a real secure-element/TPM-backed key store, addressed
    via a PKCS#11 URI (e.g. ``pkcs11:token=edge01;object=mqtt-client-key``)
    resolved through a vendor PKCS#11 module. This class does NOT
    implement PKCS#11 itself and does not talk to any hardware — there is
    none available in this environment to test against (same constraint
    documented on `engine.watchdog.LinuxHardwareWatchdog`). What it does
    do: define the exact shape a real implementation must have, and
    refuse — loudly, via `KeyStorageUnavailable` — to hand back a key
    reference when the module/URI it needs aren't actually configured,
    rather than quietly behaving like a `FileKeyStore`.
    """

    def __init__(self, *, pkcs11_uri: str | None, module_path: str | Path | None) -> None:
        self._pkcs11_uri = pkcs11_uri
        self._module_path = Path(module_path) if module_path else None

    def get_client_key_reference(self) -> str:
        if not self._pkcs11_uri or self._module_path is None or not self._module_path.exists():
            raise KeyStorageUnavailable(
                "Pkcs11KeyStore requires a real PKCS#11 module and URI; got "
                f"module_path={self._module_path!r} pkcs11_uri={self._pkcs11_uri!r}. "
                "This is a scaffold — see engine/key_storage.py module docstring."
            )
        return self._pkcs11_uri

    def is_extractable(self) -> bool:
        return False
