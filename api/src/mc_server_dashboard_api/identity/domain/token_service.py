"""The ``TokenService`` Port: issue and verify session tokens (FR-AUTH-2).

A Port so the domain never depends on a concrete token technology; the JWT
adapter lives in ``identity.adapters`` and is parameterised by
``auth.token.*`` at the edge (CONFIGURATION.md Section 5.3). Two token kinds with
different shapes (ARCHITECTURE.md Section 5.1):

- **Access token** — a short-lived, self-describing JWT carrying the user id.
  It is verified by signature/expiry alone; nothing is persisted.
- **Refresh token** — a long-lived opaque random secret. Only its *hash* is
  persisted (DATABASE.md Section 4); the Port mints the secret and exposes a
  deterministic hash so a presented token can be checked against the stored row.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class IssuedRefreshToken:
    """A freshly minted refresh token: the secret to return, and its stored hash.

    ``secret`` is handed to the caller exactly once; ``token_hash`` is what the
    ``refresh_token`` row stores (DATABASE.md Section 4). The plaintext secret is
    never persisted.
    """

    secret: str
    token_hash: str


class TokenService(abc.ABC):
    """Port: mint short-lived access tokens and opaque refresh-token secrets."""

    @abc.abstractmethod
    def issue_access_token(self, user_id: UserId) -> str:
        """Return a signed access token carrying ``user_id`` and standard claims."""

    @abc.abstractmethod
    def verify_access_token(self, token: str) -> UserId:
        """Return the subject of a valid ``token``.

        Raises :class:`InvalidAccessTokenError` if the signature, format, or
        expiry check fails.
        """

    @abc.abstractmethod
    def issue_refresh_token(self) -> IssuedRefreshToken:
        """Mint a new opaque refresh-token secret and its storable hash."""

    @abc.abstractmethod
    def hash_refresh_token(self, secret: str) -> str:
        """Return the stored-hash form of a presented refresh-token ``secret``."""
