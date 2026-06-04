"""Value objects for the identity context: ids and validated credentials.

Pure, immutable, standard-library only. Ids wrap a UUID (the application-
generated primary key, DATABASE.md Section 2). ``Username`` and ``EmailAddress``
enforce the minimal invariants the schema relies on and carry the
case-insensitive uniqueness rule (DATABASE.md Section 4) into the domain so two
spellings of the same identity compare equal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from mc_server_dashboard_api.identity.domain.errors import (
    InvalidEmailError,
    InvalidUsernameError,
)


@dataclass(frozen=True)
class UserId:
    """The identity of a :class:`~.entities.User` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> UserId:
        """Generate a fresh, random user id."""

        return cls(uuid.uuid4())


@dataclass(frozen=True)
class RefreshTokenId:
    """The identity of a :class:`~.entities.RefreshToken` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> RefreshTokenId:
        """Generate a fresh, random refresh-token id."""

        return cls(uuid.uuid4())


@dataclass(frozen=True)
class Username:
    """A user's login name.

    Surrounding whitespace is trimmed and a blank name is rejected. Equality and
    hashing use a case-folded key so uniqueness is case-insensitive
    (DATABASE.md Section 4) while ``value`` preserves the display spelling.
    """

    value: str = field(compare=False)
    _key: str = field(init=False, repr=False, compare=True)

    def __init__(self, value: str) -> None:
        trimmed = value.strip()
        if not trimmed:
            raise InvalidUsernameError("username must not be blank")
        object.__setattr__(self, "value", trimmed)
        object.__setattr__(self, "_key", trimmed.casefold())

    @property
    def key(self) -> str:
        """The case-folded identity key, stable across spelling variants.

        Brute-force / lockout state keys on this (not ``value``) so failures
        spread across casings of one username aggregate (SECURITY.md Section 2).
        """

        return self._key


@dataclass(frozen=True)
class EmailAddress:
    """A user's email address, normalized to lowercase.

    A minimal structural check (a single ``@`` with non-empty local and domain
    parts) keeps the value object honest without re-implementing full RFC 5322;
    deeper validation, if needed, belongs at the registration edge.
    """

    value: str

    def __init__(self, value: str) -> None:
        normalized = value.strip().lower()
        local, _, domain = normalized.partition("@")
        if not local or not domain or "@" in domain:
            raise InvalidEmailError("email must be of the form local@domain")
        object.__setattr__(self, "value", normalized)
