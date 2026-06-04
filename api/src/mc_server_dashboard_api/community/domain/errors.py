"""Domain errors for the community context.

Raised by the pure domain (value objects, entities) on invariant violations.
They carry no framework type and are translated to transport errors at the edge.
"""

from __future__ import annotations


class CommunityError(Exception):
    """Base class for community-domain invariant violations."""


class InvalidCommunityNameError(CommunityError):
    """A community name failed its validation rules (e.g. blank)."""


class InvalidRoleNameError(CommunityError):
    """A role name failed its validation rules (e.g. blank)."""


class InvalidPermissionError(CommunityError):
    """A permission code is not a well-formed ``<resource>:<action>`` string.

    This is the lightweight shape check only; the authoritative catalog
    (REQUIREMENTS.md Appendix A) is checked separately by
    :func:`~mc_server_dashboard_api.community.domain.permissions.require_known_permission`.
    """


class UnknownPermissionError(CommunityError):
    """A shape-valid permission code is absent from the authoritative catalog.

    Raised when an operation code (REQUIREMENTS.md Appendix A) is checked against
    the catalog and not found — e.g. a role/grant carrying a code no operation
    uses, or a permission requirement naming a non-existent operation.
    """


class CommunityAlreadyExistsError(CommunityError):
    """Creation hit the community name uniqueness constraint."""


class RoleAlreadyExistsError(CommunityError):
    """Creation hit the per-community role name uniqueness constraint."""


class MembershipAlreadyExistsError(CommunityError):
    """A user is already a member of the community (the ``UNIQUE`` pair)."""


class ResourceGrantAlreadyExistsError(CommunityError):
    """A grant already exists for the ``(user, resource_type, resource_id)`` triple."""
