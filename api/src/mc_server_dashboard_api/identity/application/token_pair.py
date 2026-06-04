"""The access + refresh token pair returned by the auth use cases (FR-AUTH-2)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenPair:
    """A freshly issued access token and the opaque refresh-token secret.

    Both are plaintext values handed to the client exactly once; the refresh
    secret's hash (not the secret) is what the ``refresh_token`` row stores.
    """

    access_token: str
    refresh_token: str
