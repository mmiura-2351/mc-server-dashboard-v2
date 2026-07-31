"""The ``TokenService`` Port: issue and verify session tokens (FR-AUTH-2).

A Port so the domain never depends on a concrete token technology; the JWT
adapter lives in ``identity.adapters`` and is parameterised by
``auth.token.*`` at the edge (CONFIGURATION.md Section 5.3). Three token kinds
with different shapes (ARCHITECTURE.md Section 5.1):

- **Access token** — a short-lived, self-describing JWT carrying the user id.
  It is verified by signature/expiry alone; nothing is persisted.
- **Refresh token** — a long-lived opaque random secret. Only its *hash* is
  persisted (DATABASE.md Section 4); the Port mints the secret and exposes a
  deterministic hash so a presented token can be checked against the stored row.
- **Download grant** — a very short-lived JWT that authenticates *one* GET of
  *one* resource, so a browser can stream a large archive straight to disk from
  a plain URL (issue #2313). ``resource`` is an opaque caller-composed string:
  the identity Port binds the grant to it without knowing what it names.
- **Download cookie** — the same resource-scoped authority as a grant, on a
  longer TTL, minted when a grant is redeemed so an interrupted transfer can be
  retried after the grant's query-string window has closed (issue #2373). It is
  a separate kind because its transport is different: it rides an ``HttpOnly``
  cookie and must never be accepted in a URL, where the grant's short TTL is the
  whole exposure bound.

The grant and cookie kinds live on this Port rather than on ones of their own so
the system keeps a single JWT adapter and a single signing key.
"""

from __future__ import annotations

import abc
import datetime as dt
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


@dataclass(frozen=True)
class IssuedDownloadGrant:
    """A freshly minted download grant and the instant it stops verifying.

    ``expires_at`` is returned rather than recomputed by the caller so the TTL has
    exactly one source of truth: the caller can advertise the deadline without
    knowing the configured TTL or re-reading the clock.
    """

    token: str
    expires_at: dt.datetime


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

    @abc.abstractmethod
    def issue_download_grant(
        self, user_id: UserId, resource: str
    ) -> IssuedDownloadGrant:
        """Mint a grant authenticating ``user_id`` for ``resource`` only."""

    @abc.abstractmethod
    def verify_download_grant(self, token: str, resource: str) -> UserId:
        """Return the subject of a grant valid for ``resource``.

        Raises :class:`InvalidDownloadGrantError` if the signature, format, or
        expiry check fails, if the token is not a download grant, or if it is
        bound to a different resource. A grant proves identity, never authority:
        the caller still runs its own authorization.
        """

    @abc.abstractmethod
    def issue_download_cookie(self, user_id: UserId, resource: str) -> str:
        """Mint a download cookie authenticating ``user_id`` for ``resource`` only.

        No deadline is returned: unlike a grant, whose ``expires_at`` a client is
        told so it can re-mint, the cookie's lifetime is the browser's ``Max-Age``
        (the configured TTL) and the JWT's own ``exp`` is what is enforced.
        """

    @abc.abstractmethod
    def verify_download_cookie(self, token: str, resource: str) -> UserId:
        """Return the subject of a download cookie valid for ``resource``.

        Raises :class:`InvalidDownloadCookieError` under the same conditions
        :meth:`verify_download_grant` raises its own error, and additionally when
        the token is a grant rather than a cookie. Identity only, exactly like a
        grant: the caller still runs its own authorization.
        """
