"""
Two-tier auth (operator/admin) per the plan. Deliberately minimal: an
in-memory/injectable user store with werkzeug password hashing. Session
cookie is Flask's signed-cookie session (fine for a single-terminal local
app; nothing here assumes a shared session store, which would matter for a
multi-instance deployment this product doesn't have — one Flask process per
terminal, per the plan's "Where the UI runs" decision).

AR-05 (Architecture Review): the previous "two shared passwords" model is
gone — every account here is per-user, added individually via add_user(),
and the audit trail this feeds (api/app.py binds session["username"] at
login time, then stamps every mutating route with that bound value — never
a request-supplied string) is bound to a real authenticated account. This
module additionally adds the other AR-05 requirement: session lockout/
backoff on repeated failed logins, so a brute-force attempt against one
account degrades to slow, loud guessing instead of unlimited fast guessing.
"""
from __future__ import annotations

import time

from werkzeug.security import check_password_hash, generate_password_hash

TIER_LEVEL = {"operator": 1, "admin": 2}

DEFAULT_MAX_FAILED_ATTEMPTS = 5
DEFAULT_LOCKOUT_SECONDS = 900  # 15 minutes


class AccountLocked(Exception):
    """Raised by verify() instead of returning None when an account is
    currently locked out — this is deliberately a distinct signal from
    "wrong password" so the API layer can tell the caller how long to
    wait, without that distinction ever depending on whether the
    *password* was right (an attacker mid-lockout learns nothing about
    whether their guess was correct)."""

    def __init__(self, retry_after_s: float):
        self.retry_after_s = retry_after_s
        super().__init__(f"account locked, retry after {retry_after_s:.0f}s")


class UserStore:
    def __init__(
        self,
        *,
        max_failed_attempts: int = DEFAULT_MAX_FAILED_ATTEMPTS,
        lockout_seconds: float = DEFAULT_LOCKOUT_SECONDS,
    ) -> None:
        self._users: dict[str, dict] = {}
        self._max_failed_attempts = max_failed_attempts
        self._lockout_seconds = lockout_seconds

    def add_user(self, username: str, password: str, tier: str) -> None:
        if tier not in TIER_LEVEL:
            raise ValueError(f"unknown tier: {tier!r}")
        self._users[username] = {
            "password_hash": generate_password_hash(password),
            "tier": tier,
            "failed_attempts": 0,
            "locked_until": 0.0,
        }

    def verify(self, username: str, password: str, *, now: float | None = None) -> str | None:
        """Returns the user's tier on success, None on "unknown user" or
        "wrong password" — those two still aren't distinguished, by
        design. Raises AccountLocked if the account is currently locked
        out, checked BEFORE the password is examined, so a locked-out
        caller gets the same signal regardless of whether they happen to
        type the right password this time."""
        now = now if now is not None else time.time()
        user = self._users.get(username)
        if user is None:
            return None

        if now < user["locked_until"]:
            raise AccountLocked(user["locked_until"] - now)

        if not check_password_hash(user["password_hash"], password):
            user["failed_attempts"] += 1
            if user["failed_attempts"] >= self._max_failed_attempts:
                user["locked_until"] = now + self._lockout_seconds
                user["failed_attempts"] = 0  # lockout window itself is the backoff; don't double-count
            return None

        user["failed_attempts"] = 0
        user["locked_until"] = 0.0
        return user["tier"]
