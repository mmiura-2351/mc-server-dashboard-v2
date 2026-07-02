"""In-memory relay state: registration + join-token minting (issue #956).

All pieces are process-local, mirroring the rest of the control plane's
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
- :class:`BedrockTunnelTable` mints and, unlike ``JoinTokenTable``, *keeps*
  the current Bedrock tunnel credential per server (issue #1544). The Bedrock
  tunnel is opened API-initiated at server-running time rather than in
  response to a relay-initiated call, so the relay has no prior waiter to
  match a Worker's QUIC dial-out against and instead asks the API to confirm
  the token (``RelayService.ValidateBedrockTunnel``) — which means the API
  must remember what it minted for the tunnel's whole lifetime, not just at
  mint time.
"""

from __future__ import annotations

import hmac
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


@dataclass(frozen=True)
class _OpenBedrockTunnel:
    """One server's currently-open Bedrock tunnel credential (issue #1544)."""

    bedrock_port: int
    token: str


class BedrockTunnelTable:
    """Tracks the current Bedrock tunnel credential per server (issue #1544).

    Unlike :class:`JoinTokenTable`, this table is stateful on the API side: the
    Bedrock tunnel is opened API-initiated (dispatched when a server reaches
    running state), not in response to a relay-initiated call the relay could
    use to pre-register its own local waiter. So when the relay later accepts
    the Worker's QUIC dial-out and calls ``RelayService.ValidateBedrockTunnel``
    to confirm the presented ``(server_id, bedrock_port, token)``, the API is
    the only party that can answer — it must remember what it minted for the
    tunnel's whole lifetime (open -> close), not just at mint time.
    """

    def __init__(self) -> None:
        self._by_server: dict[str, _OpenBedrockTunnel] = {}

    def open(self, *, server_id: str, bedrock_port: int) -> str:
        """Mint a fresh token for ``server_id``'s tunnel, replacing any prior one."""

        token = secrets.token_hex(_TOKEN_BYTES)
        self._by_server[server_id] = _OpenBedrockTunnel(
            bedrock_port=bedrock_port, token=token
        )
        return token

    def close(self, *, server_id: str) -> None:
        """Forget ``server_id``'s tunnel credential; a later ``validate`` fails.

        Idempotent — closing a server with no open tunnel is a no-op.
        """

        self._by_server.pop(server_id, None)

    def validate(self, *, server_id: str, bedrock_port: int, token: str) -> bool:
        """Whether ``(server_id, bedrock_port, token)`` matches the open tunnel."""

        current = self._by_server.get(server_id)
        return (
            current is not None
            and current.bedrock_port == bedrock_port
            and hmac.compare_digest(current.token, token)
        )
