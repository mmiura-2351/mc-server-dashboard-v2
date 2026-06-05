"""backups: widen ``ck_backup_source`` to ``uploaded``

The 0006 migration pinned the ``backup`` table's ``source`` CHECK to
``('manual', 'scheduled', 'event')``. Issue #281 adds the backup-upload endpoint,
which records an off-host archive as a new backup row with ``source='uploaded'``
-- so the live CHECK must be widened or an uploaded-backup INSERT would violate
``ck_backup_source``. This migration recreates the CHECK with the four values the
model now renders.

The constraint keeps the SAME explicit ``ck_backup_source`` name (pinned on the
model), so the name-sync test stays quiet and an Alembic autogenerate sees no
rename. Recreating a CHECK is a drop + add: the new clause matches the model's
rendering exactly (``IN`` list in model order, single-quoted values), mirroring
the 0010 ``server_type`` widening.

Downgrade restores the 0006 three-value CHECK. Any ``uploaded`` rows present at
downgrade would violate it; there are none in the documented flow before this
change shipped.

Numbering note (issue #281): authored as ``0012`` off ``0011_user_active``. The
concurrent groups PR (#279) merged first as ``0012_player_groups``, so this
rebased to ``0013`` chaining off it (a trivial down_revision bump, expected per
the issue).

Revision ID: 0013_backup_source_uploaded
Revises: 0012_player_groups
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_backup_source_uploaded"
down_revision: str | None = "0012_player_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_backup_source"
_NEW_CHECK = "source IN ('manual', 'scheduled', 'event', 'uploaded')"
_OLD_CHECK = "source IN ('manual', 'scheduled', 'event')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "backup", type_="check")
    op.create_check_constraint(_CONSTRAINT, "backup", _NEW_CHECK)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "backup", type_="check")
    op.create_check_constraint(_CONSTRAINT, "backup", _OLD_CHECK)
