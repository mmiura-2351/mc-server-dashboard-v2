"""community: backfill audit:read onto existing preset Owner roles

The ``audit:read`` permission was added to :data:`COMMUNITY_PERMISSIONS` (the seed
for *new* communities' Owner role) after early communities were provisioned, so
their preset Owner roles lack it. This data migration backfills the permission
onto every existing **preset** Owner role (``is_preset = true`` AND
``name = 'Owner'``) whose permission array does not already contain it (issue
#131). Non-preset roles and custom roles are never touched: the Owner preset is
the only role derived from the catalog.

Idempotent on both directions: upgrade only appends to rows missing the code, so
re-running is a no-op; downgrade only removes it from preset Owner rows that have
it. Downgrade is a best-effort inverse — it cannot tell a backfilled code from
one an operator added by hand, so it strips it from all preset Owner roles
(acceptable: the catalog seeds it anyway, so a fresh seed re-adds it).

Revision ID: 0008_owner_audit_read_backfill
Revises: 0007_audit_log
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_owner_audit_read_backfill"
down_revision: str | None = "0007_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION = "audit:read"
_OWNER_ROLE_NAME = "Owner"


def upgrade() -> None:
    # array_append only on preset Owner rows that lack the code (idempotent).
    op.execute(
        f"""
        UPDATE role
        SET permissions = array_append(permissions, '{_PERMISSION}')
        WHERE is_preset = true
          AND name = '{_OWNER_ROLE_NAME}'
          AND NOT ('{_PERMISSION}' = ANY(permissions))
        """
    )


def downgrade() -> None:
    # array_remove strips every occurrence; only preset Owner rows are touched.
    op.execute(
        f"""
        UPDATE role
        SET permissions = array_remove(permissions, '{_PERMISSION}')
        WHERE is_preset = true
          AND name = '{_OWNER_ROLE_NAME}'
          AND '{_PERMISSION}' = ANY(permissions)
        """
    )
