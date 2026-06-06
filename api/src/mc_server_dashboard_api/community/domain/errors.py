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


class OwnerUserNotFoundError(CommunityError):
    """The initial owner named for a community provisioning is not a known user.

    Provisioning validates the owner against the :class:`UserDirectory` Port
    (FR-COMM-2); an unknown user id raises this rather than violating the
    ``membership.user_id`` foreign key.
    """


class MemberUserNotFoundError(CommunityError):
    """The user named for a manual member-add is not a known user.

    Adding a member validates the user against the :class:`UserDirectory` Port
    (FR-MEM-1); an unknown user id raises this rather than violating the
    ``membership.user_id`` foreign key.
    """


class CommunityNotFoundError(CommunityError):
    """The targeted community does not exist (read/update/delete on a missing id)."""


class MembershipNotFoundError(CommunityError):
    """The targeted user is not a member of the community.

    Raised by remove-member / role assignment when the named user has no
    membership in the community.
    """


class RoleNotFoundError(CommunityError):
    """The targeted role does not exist in the community.

    Raised when assigning/unassigning a role the community does not own — either
    a wholly unknown role id or, security-critically, a role belonging to a
    *different* community (cross-community assignment, FR-AUTHZ-4). The
    ``membership_role`` FK accepts any role id, so the use case must validate the
    role's ``community_id`` matches; a mismatch is reported as not-found, giving
    no signal about another community's roles.
    """


class PresetRoleNotEditableError(CommunityError):
    """An attempt was made to edit or delete a seeded preset role.

    The preset Owner role (FR-COMM-4) is what keeps a community administrable: its
    permission set must remain the full community-scoped catalog, and it must
    always exist. The simplest honest guard (issue #71) is to make preset roles
    immutable and undeletable; editing or deleting one raises this.
    """


class InvalidGrantResourceTypeError(CommunityError):
    """A resource grant named a ``resource_type`` outside the M1 catalog.

    ``resource_type`` is a CHECK-constrained enum (DATABASE.md Section 6; ``server``
    in M1). A value outside it is rejected in the use case rather than tripping the
    database CHECK.
    """


class GrantTargetNotMemberError(CommunityError):
    """The user a grant targets is not a member of the community.

    A resource grant attaches permissions to a *member* (FR-AUTHZ-2). Granting to a
    non-member raises this rather than creating a grant that no membership backs.
    """


class GrantResourceNotFoundError(CommunityError):
    """The resource a grant targets does not exist in the community.

    A resource grant attaches permissions to a specific resource (FR-AUTHZ-2).
    Granting on a ``resource_id`` that names no resource in the community (a
    fabricated id, or one from another community) raises this rather than
    persisting a dangling grant that surfaces as a ghost in the grant listing and
    ``me/permissions`` (issue #361). Reported as not-found at the edge, mirroring
    the no-existence-signal posture for resources outside the community.
    """


class ResourceGrantNotFoundError(CommunityError):
    """The targeted resource grant does not exist in the community.

    Raised by revoke-grant when the named grant id is unknown or, security-
    critically, belongs to a *different* community (cross-community access, the
    same posture as :class:`RoleNotFoundError`): reported as not-found so no signal
    about another community's grants leaks.
    """


class LastOwnerRemovalError(CommunityError):
    """Removing this member would leave the community with no Owner-role holder.

    The preset Owner role (FR-COMM-4) is what keeps a community administrable; if
    the member being removed is the only one holding it, the community would be
    orphaned. The remove-member use case rejects this rather than allow it.
    """
