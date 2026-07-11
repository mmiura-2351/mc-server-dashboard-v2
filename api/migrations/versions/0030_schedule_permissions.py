"""scheduler permissions: backfill schedule:read/schedule:manage onto Owner

Backfills the new ``schedule:read`` and ``schedule:manage`` permission codes
(epic #649, issue #1837) onto every existing **preset** Owner role, mirroring
0008/0012/0017/0019: the codes were added to the catalog (the seed for *new*
communities' Owner role), so already-provisioned communities' Owner roles lack
them and this data backfill grants them. Only preset Owner rows
(``is_preset = true`` AND ``name = 'Owner'``) are touched; a non-preset role or a
differently-named preset role is left untouched. Idempotent both ways.

The ``schedule`` / ``schedule_run`` tables themselves were created by 0029; this
migration carries no schema change.

Revision ID: 0030_schedule_permissions
Revises: 0029_schedules
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030_schedule_permissions"
down_revision: str | None = "0029_schedules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER_ROLE_NAME = "Owner"
_NEW_PERMISSIONS = ("schedule:read", "schedule:manage")


def upgrade() -> None:
    for perm in _NEW_PERMISSIONS:
        op.execute(
            f"""
            UPDATE role
            SET permissions = array_append(permissions, '{perm}')
            WHERE is_preset = true
              AND name = '{_OWNER_ROLE_NAME}'
              AND NOT ('{perm}' = ANY(permissions))
            """
        )


def downgrade() -> None:
    for perm in _NEW_PERMISSIONS:
        op.execute(
            f"""
            UPDATE role
            SET permissions = array_remove(permissions, '{perm}')
            WHERE is_preset = true
              AND name = '{_OWNER_ROLE_NAME}'
              AND '{perm}' = ANY(permissions)
            """
        )
