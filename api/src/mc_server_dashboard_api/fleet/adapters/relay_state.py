"""In-memory relay state: registration + single-use join tokens (issue #956).

Both pieces are process-local, mirroring the rest of the control plane's
in-memory adapters (ControlPlaneState, InMemoryWorkerRegistry): a single API
process owns the relay's gRPC calls at this scale (NFR-SCALE-1), so a plain dict
is sufficient. Mutations are synchronous, non-blocking dict work on the one
asyncio event loop, so no lock is required under cooperative scheduling.

- :class:`RelayRegistration` holds the relay's advertised tunnel endpoint and CA
  PEM from its last ``Register`` call (last-writer-wins, one relay per
  deployment — RELAY.md Section 6). The API reads it to fill ``TunnelDial``.
- :class:`JoinTokenTable` mints single-use 128-bit tokens with a short TTL
  (RELAY.md Sections 4, 5): each maps to the server a ``ResolveJoin`` matched,
  is consumed on first use, and expires by the clock. Expired entries are swept
  on mint so a flood of never-consumed tokens cannot grow the table unbounded.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.clock import Clock

# A 128-bit token (RELAY.md Section 5), rendered as 32 lowercase hex chars.
_TOKEN_BYTES = 16


@dataclass(frozen=True)
class RegisteredRelay:
    """The relay's advertised tunnel endpoint and CA PEM (RELAY.md Section 6)."""

    endpoint: str
    ca_pem: str


class RelayRegistration:
    """Last-writer-wins store of the single relay's registration."""

    def __init__(self) -> None:
        self._current: RegisteredRelay | None = None

    def set(self, *, endpoint: str, ca_pem: str) -> None:
        """Record the relay's endpoint/CA, replacing any prior registration.

        One relay per deployment (RELAY.md Section 6): a second ``Register`` from
        a different relay instance simply replaces the stored values.
        """

        self._current = RegisteredRelay(endpoint=endpoint, ca_pem=ca_pem)

    def current(self) -> RegisteredRelay | None:
        """Return the current registration, or ``None`` if no relay has registered."""

        return self._current


class JoinTokenTable:
    """Single-use, TTL-bounded join tokens keyed to a server (RELAY.md Section 5)."""

    def __init__(self, *, clock: Clock, ttl: dt.timedelta) -> None:
        self._clock = clock
        self._ttl = ttl
        # token -> (server_id, expires_at).
        self._tokens: dict[str, tuple[str, dt.datetime]] = {}

    def mint(self, *, server_id: str) -> str:
        """Mint a fresh single-use token for ``server_id`` and return it.

        Sweeps expired entries first so the table cannot grow unbounded under a
        flood of never-consumed tokens (the dial-back may never arrive — RELAY.md
        Section 10).
        """

        now = self._clock.now()
        self._prune(now)
        token = secrets.token_hex(_TOKEN_BYTES)
        self._tokens[token] = (server_id, now + self._ttl)
        return token

    def consume(self, token: str) -> str | None:
        """Consume ``token`` and return its server id, or ``None`` if invalid.

        Single-use: a consumed token is removed, so a replay returns ``None``.
        An unknown or expired token also returns ``None`` (RELAY.md Section 5).
        """

        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        server_id, expires_at = entry
        if self._clock.now() > expires_at:
            return None
        return server_id

    def size(self) -> int:
        """Return the number of live token entries (test/diagnostic helper)."""

        return len(self._tokens)

    def _prune(self, now: dt.datetime) -> None:
        expired = [
            token for token, (_, expires_at) in self._tokens.items() if now > expires_at
        ]
        for token in expired:
            del self._tokens[token]
