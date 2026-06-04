"""JWT + opaque-secret adapter for the :class:`TokenService` Port (FR-AUTH-2).

The single M1 ``TokenService`` adapter (ARCHITECTURE.md Section 5.1), wired at the
edge from ``auth.token.*`` (CONFIGURATION.md Section 5.3):

- **Access tokens** are JWTs signed with ``auth.token.algorithm`` (HS256 default;
  RS256 supported by passing the matching key material). Claims: ``sub`` (the
  user id), ``iat``, and ``exp`` (``access_ttl`` after issue). No ``iss``/``aud``
  — this is a single-issuer, single-audience system, so they would add no check.
- **Refresh tokens** are opaque high-entropy random secrets, never JWTs. Only
  their SHA-256 hash is stored (DATABASE.md Section 4); a fast hash is correct
  here because the input is already 256+ bits of entropy, and it keeps the
  ``UNIQUE(token_hash)`` lookup a plain equality match.

The signing key is held in memory only and never logged.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

import jwt

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import InvalidAccessTokenError
from mc_server_dashboard_api.identity.domain.token_service import (
    IssuedRefreshToken,
    TokenService,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId

# Bytes of entropy for the opaque refresh secret (token_urlsafe argument).
_REFRESH_SECRET_BYTES = 32


class JwtTokenService(TokenService):
    """:class:`TokenService` adapter: PyJWT access tokens + opaque refresh secrets."""

    def __init__(
        self,
        *,
        signing_key: str,
        algorithm: str,
        access_ttl: dt.timedelta,
        clock: Clock,
    ) -> None:
        self._signing_key = signing_key
        self._algorithm = algorithm
        self._access_ttl = access_ttl
        self._clock = clock

    def issue_access_token(self, user_id: UserId) -> str:
        now = self._clock.now()
        claims = {
            "sub": str(user_id.value),
            "iat": int(now.timestamp()),
            "exp": int((now + self._access_ttl).timestamp()),
        }
        return jwt.encode(claims, self._signing_key, algorithm=self._algorithm)

    def verify_access_token(self, token: str) -> UserId:
        try:
            # Signature is checked by PyJWT; expiry is checked against the
            # injected Clock (not PyJWT's wall clock) so the domain has a single,
            # testable source of time.
            claims = jwt.decode(
                token,
                self._signing_key,
                algorithms=[self._algorithm],
                options={"verify_exp": False},
            )
            if int(self._clock.now().timestamp()) >= int(claims["exp"]):
                raise InvalidAccessTokenError
            return UserId(uuid.UUID(claims["sub"]))
        except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
            raise InvalidAccessTokenError from exc

    def issue_refresh_token(self) -> IssuedRefreshToken:
        secret = secrets.token_urlsafe(_REFRESH_SECRET_BYTES)
        return IssuedRefreshToken(
            secret=secret, token_hash=self.hash_refresh_token(secret)
        )

    def hash_refresh_token(self, secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()
