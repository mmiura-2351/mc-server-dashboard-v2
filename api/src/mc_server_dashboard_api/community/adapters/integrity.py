"""Integrity-constraint -> domain-error translation for the community adapters.

Unique violations from PostgreSQL are translated to the same typed domain error a
use-case pre-check raises, so a caller that reaches the constraint gets the
promised 409 instead of a raw ``IntegrityError`` (500). The four backstops are
``uq_community_name``, ``uq_role_community_name``,
``uq_membership_user_community`` and ``uq_resource_grant_user_resource`` (all
migration 0004).

Shared by two kinds of call site, because *when* a violation surfaces depends on
the statement shape: an INSERT staged via ``session.add`` flushes at commit -- or
at ``ProvisionCommunity``'s mid-transaction flush -- so
:class:`~mc_server_dashboard_api.community.adapters.unit_of_work.SqlAlchemyUnitOfWork`
translates in ``flush`` and ``commit``; an UPDATE executes -- and violates --
immediately inside the transaction, one statement before any commit is reached,
so the community and role repositories translate at their ``update`` execute
sites. That second kind is why the map lives here rather than in the unit of
work, where it started: the unit of work imports the repositories, so the
repositories cannot import it back. ``servers/adapters/integrity.py`` is split
out for the same reason and is the pattern this mirrors.

``UpdateRole`` has no name-clash pre-check, so its rename is not a race but the
ordinary user action: until the execute site was wrapped it was a deterministic
500, while ``community/api/roles.py`` caught a ``RoleAlreadyExistsError`` that
nothing on that path could raise (issue #2611).

**Why this context maps its own constraints instead of sharing the servers map.**
Per-context is already the convention, and by more than one example:
``identity/adapters/integrity.py`` holds its own two-entry map and is likewise
shared by its unit of work's commit path and its repository's eager Core UPDATE
-- the same two call-site kinds, split out for the same reason. Of the three
contexts that translate at all, community was simply the one that had neither the
split-out module nor the UPDATE-site wrap. Sharing a single map instead would
mean one function importing every context's ``domain.errors`` and raising each
context's errors into the others' use cases, for no gain: the constraint
namespaces are disjoint, so no entry would ever be reached from more than one
context. (The import-linter contracts do not forbid the import -- they bar the
community *domain* and *application* from reaching into ``servers``, and this is
the adapter layer, which already reaches ``servers.adapters.models`` for the
resource-existence check. What keeps the maps apart is the coupling, not a
contract.)

**Foreign keys are deliberately absent, and their absence is not a claim that
they are unreachable.** Every FK in migration 0004 --
``fk_role_community_id_community``, ``fk_membership_user_id_user``,
``fk_membership_community_id_community``,
``fk_membership_role_membership_id_membership``,
``fk_membership_role_role_id_role``, ``fk_resource_grant_user_id_user``,
``fk_resource_grant_community_id_community`` -- plus the composite
``pk_membership_role`` is violable only by a *concurrent delete* of the parent
row, or for the PK a concurrent double assignment: they are all
``ON DELETE CASCADE``, every use case pre-reads what it writes to, and
``AssignRole`` pre-checks the assignment for idempotence. Translating one means
deciding a typed error *and* an HTTP status for "the community / user /
membership vanished mid-write", which none of these routes maps today -- a
decision per constraint rather than a map entry, and a different defect from the
deterministic one above. Note also that a map entry alone would not be enough:
these FKs are not DEFERRABLE, so each violation surfaces at whichever statement
ends up flushing the staged row, and that statement has to be inside a wrap for
the entry to be reached at all (issue #2612).
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    MembershipAlreadyExistsError,
    ResourceGrantAlreadyExistsError,
    RoleAlreadyExistsError,
)

_COMMUNITY_NAME_CONSTRAINTS = frozenset({"uq_community_name"})
_ROLE_NAME_CONSTRAINTS = frozenset({"uq_role_community_name"})
_MEMBERSHIP_CONSTRAINTS = frozenset({"uq_membership_user_community"})
_RESOURCE_GRANT_CONSTRAINTS = frozenset({"uq_resource_grant_user_resource"})


def translate_integrity_error(exc: IntegrityError) -> None:
    """Raise the matching domain error for a known unique violation, else return.

    An unrecognised violation is left to the caller to re-raise as-is.
    """

    constraint = _constraint_name(exc)
    if constraint in _COMMUNITY_NAME_CONSTRAINTS:
        raise CommunityAlreadyExistsError(str(constraint)) from exc
    if constraint in _ROLE_NAME_CONSTRAINTS:
        raise RoleAlreadyExistsError(str(constraint)) from exc
    if constraint in _MEMBERSHIP_CONSTRAINTS:
        raise MembershipAlreadyExistsError(str(constraint)) from exc
    if constraint in _RESOURCE_GRANT_CONSTRAINTS:
        raise ResourceGrantAlreadyExistsError(str(constraint)) from exc


def _constraint_name(exc: IntegrityError) -> str | None:
    """Extract the violated constraint name from the wrapped driver error.

    The constraint name lives on the asyncpg ``UniqueViolationError`` underneath
    the SQLAlchemy wrapper (``exc.orig`` is the DBAPI shim; its ``__cause__`` is
    the asyncpg error).
    """

    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None
