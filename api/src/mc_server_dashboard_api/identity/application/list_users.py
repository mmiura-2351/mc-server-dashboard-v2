"""ListUsers use case: the platform-admin paginated user listing (issue #278).

Reads a page of users (ordered by ``created_at``) and the total count so the edge
can render pagination. Read-only: it opens the unit of work, queries, and never
commits.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class UserPage:
    """A page of users plus the total row count (for pagination)."""

    users: list[User]
    total: int


@dataclass(frozen=True)
class ListUsers:
    """List users for the platform-admin user-administration surface."""

    uow: UnitOfWork

    async def __call__(self, *, limit: int, offset: int) -> UserPage:
        async with self.uow:
            users = await self.uow.users.list_page(limit=limit, offset=offset)
            total = await self.uow.users.count_all()
        return UserPage(users=users, total=total)
