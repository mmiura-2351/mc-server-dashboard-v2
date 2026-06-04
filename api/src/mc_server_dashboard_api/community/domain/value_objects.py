"""Value objects for the community context: ids, names, and permission codes.

Pure, immutable, standard-library only. Ids wrap a UUID (the application-
generated primary key, DATABASE.md Section 2). ``UserId`` is a *foreign*
reference: the community domain holds the user only as an id value and never
imports the identity domain (the FK lives at the persistence layer, DATABASE.md
Section 5). ``CommunityName`` / ``RoleName`` enforce the minimal invariants the
schema relies on, and ``Permission`` enforces the ``<resource>:<action>`` shape
(REQUIREMENTS.md Appendix A) — a lightweight check only; validation against the
authoritative catalog lands with the ``PermissionChecker`` (#68).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.community.domain.errors import (
    InvalidCommunityNameError,
    InvalidPermissionError,
    InvalidRoleNameError,
)


@dataclass(frozen=True)
class CommunityId:
    """The identity of a :class:`~.entities.Community` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> CommunityId:
        """Generate a fresh, random community id."""

        return cls(uuid.uuid4())


@dataclass(frozen=True)
class MembershipId:
    """The identity of a :class:`~.entities.Membership` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> MembershipId:
        """Generate a fresh, random membership id."""

        return cls(uuid.uuid4())


@dataclass(frozen=True)
class RoleId:
    """The identity of a :class:`~.entities.Role` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> RoleId:
        """Generate a fresh, random role id."""

        return cls(uuid.uuid4())


@dataclass(frozen=True)
class ResourceGrantId:
    """The identity of a :class:`~.entities.ResourceGrant` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> ResourceGrantId:
        """Generate a fresh, random resource-grant id."""

        return cls(uuid.uuid4())


@dataclass(frozen=True)
class UserId:
    """A foreign reference to an identity-context user, by id value only.

    The community domain never imports the identity domain (DATABASE.md
    Section 5): a user is referenced here purely as a UUID. The
    persistence-layer FK to ``user.id`` enforces referential integrity.
    """

    value: uuid.UUID


@dataclass(frozen=True)
class CommunityName:
    """A community's display name.

    Surrounding whitespace is trimmed and a blank name is rejected; the schema's
    ``UNIQUE(name)`` (DATABASE.md Section 5) handles uniqueness exactly.
    """

    value: str

    def __init__(self, value: str) -> None:
        trimmed = value.strip()
        if not trimmed:
            raise InvalidCommunityNameError("community name must not be blank")
        object.__setattr__(self, "value", trimmed)


@dataclass(frozen=True)
class RoleName:
    """A role's name, unique within its community (DATABASE.md Section 5).

    Surrounding whitespace is trimmed and a blank name is rejected.
    """

    value: str

    def __init__(self, value: str) -> None:
        trimmed = value.strip()
        if not trimmed:
            raise InvalidRoleNameError("role name must not be blank")
        object.__setattr__(self, "value", trimmed)


@dataclass(frozen=True)
class Permission:
    """An operation permission code of the form ``<resource>:<action>``.

    Only the *shape* is validated here (a single ``:`` with non-empty resource
    and action and no surrounding whitespace) — enough to keep stored sets
    honest. Whether the code is in the authoritative catalog (REQUIREMENTS.md
    Appendix A) is the ``PermissionChecker``'s job, landing with #68.
    """

    value: str

    def __init__(self, value: str) -> None:
        resource, sep, action = value.partition(":")
        if not sep or not resource or not action or ":" in action:
            raise InvalidPermissionError(
                "permission must be of the form <resource>:<action>"
            )
        if value != value.strip():
            raise InvalidPermissionError(
                "permission must not have surrounding whitespace"
            )
        object.__setattr__(self, "value", value)
