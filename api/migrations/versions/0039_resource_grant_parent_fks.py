"""resource_grant: enforce the membership and server it belongs to

Issue #3216: ``CreateGrant`` checked that the target is a member and that the
server exists, but nothing held either fact until its INSERT committed. A member
removal or server deletion that committed in between swept the grants it could
see and left the late grant behind -- for a removed member, a grant that
re-adding the user silently reactivated (FR-MEM-3).

This migration makes both relationships foreign keys, ``ON DELETE CASCADE``:

- ``fk_resource_grant_user_id_membership``: ``(user_id, community_id)`` ->
  ``membership(user_id, community_id)`` (backed by
  ``uq_membership_user_community``);
- ``fk_resource_grant_resource_id_server``: ``resource_id`` -> ``server.id``.
  ``server`` is the only ``resource_type`` ``ck_resource_grant_resource_type``
  admits, so the reference is no longer polymorphic in practice.

Rows that violate them are deleted first. They are by definition ghost
permissions -- a grant whose membership was removed, or whose server was
deleted, is exactly what the removal and deletion were meant to revoke -- so no
data a user could still rely on is lost.

Downgrade drops the two foreign keys; the deleted ghost rows are not restored.

Revision ID: 0039_resource_grant_parent_fks
Revises: 0038_backup_health_unreadable
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0039_resource_grant_parent_fks"
down_revision: str | None = "0038_backup_health_unreadable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MEMBERSHIP_FK = "fk_resource_grant_user_id_membership"
_SERVER_FK = "fk_resource_grant_resource_id_server"


def upgrade() -> None:
    op.execute(
        "DELETE FROM resource_grant g WHERE NOT EXISTS ("
        "SELECT 1 FROM membership m "
        "WHERE m.user_id = g.user_id AND m.community_id = g.community_id)"
    )
    op.execute(
        "DELETE FROM resource_grant g WHERE NOT EXISTS ("
        "SELECT 1 FROM server s WHERE s.id = g.resource_id)"
    )
    op.create_foreign_key(
        _MEMBERSHIP_FK,
        "resource_grant",
        "membership",
        ["user_id", "community_id"],
        ["user_id", "community_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        _SERVER_FK,
        "resource_grant",
        "server",
        ["resource_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(_SERVER_FK, "resource_grant", type_="foreignkey")
    op.drop_constraint(_MEMBERSHIP_FK, "resource_grant", type_="foreignkey")
