"""In-memory relay state: registration + join-token minting (issue #956).

Both pieces are process-local, mirroring the rest of the control plane's
in-memory adapters (ControlPlaneState, InMemoryWorkerRegistry): a single API
process owns the relay's gRPC calls at this scale (NFR-SCALE-1).

- :class:`RelayRegistration` holds the relay's advertised tunnel endpoint and CA
  PEM from its last ``Register`` call (last-writer-wins, one relay per
  deployment — RELAY.md Section 6). The API reads it to fill ``TunnelDial``.
- :class:`JoinTokenTable` mints single-use 128-bit tokens (RELAY.md Sections 4,
  5). Single-use and TTL enforcement live *relay-side* (the tunnel listener
  validates the token in tokens.go); the API only needs to mint a fresh,
  unguessable value to carry into the ``TunnelDial`` command — it never consumes
  or validates the token back, so no API-side table is kept.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

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
    """Mints single-use 128-bit join tokens (RELAY.md Section 5).

    Stateless on the API side: single-use and TTL enforcement live relay-side
    (tokens.go), and the API never validates a minted token back, so there is
    nothing to store. ``mint`` simply returns a fresh, unguessable value to carry
    into the ``TunnelDial`` command.
    """

    def mint(self) -> str:
        """Return a fresh single-use join token (128 bits, 32 hex chars)."""

        return secrets.token_hex(_TOKEN_BYTES)
