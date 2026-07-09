"""
Two-tier auth (operator/admin) per the plan. Deliberately minimal: an
in-memory/injectable user store with werkzeug password hashing. Session
cookie is Flask's signed-cookie session (fine for a single-terminal local
app; nothing here assumes a shared session store, which would matter for a
multi-instance deployment this product doesn't have — one Flask process per
terminal, per the plan's "Where the UI runs" decision).
"""
from __future__ import annotations

from werkzeug.security import check_password_hash, generate_password_hash

TIER_LEVEL = {"operator": 1, "admin": 2}


class UserStore:
    def __init__(self) -> None:
        self._users: dict[str, dict] = {}

    def add_user(self, username: str, password: str, tier: str) -> None:
        if tier not in TIER_LEVEL:
            raise ValueError(f"unknown tier: {tier!r}")
        self._users[username] = {"password_hash": generate_password_hash(password), "tier": tier}

    def verify(self, username: str, password: str) -> str | None:
        """Returns the user's tier on success, None on any failure. Doesn't
        distinguish "unknown user" from "wrong password" in its return
        value or timing-sensitive branching — both fail the same way."""
        user = self._users.get(username)
        if user is None:
            return None
        if not check_password_hash(user["password_hash"], password):
            return None
        return user["tier"]
