"""backup: widen ``ck_backup_health`` to ``unreadable``

Issue #2374 adds a fourth ``BackupHealth`` value, ``unreadable``, for a backup
whose archive the integrity sweep could not read back at all (the store cannot
produce its bytes, or holds none). Until now the sweep folded that finding into
``quarantined``, which means "the archived world is structurally corrupt" and is
still restorable with the operator override (#703); an archive whose bytes are
gone is not restorable at all, so the two verdicts are kept apart. The live CHECK
must admit the new value or the sweep's write would violate ``ck_backup_health``.
This migration recreates the CHECK with the four values the model now renders.

Existing rows are left exactly as they are (owner decision on #2374): a
``quarantined`` row may mean either finding until the next integrity sweep, which
re-reads every backup and re-classifies it. Rewriting them to ``unknown`` here was
rejected: it would make known-damaged backups look unexamined in the meantime.

The constraint keeps the SAME explicit ``ck_backup_health`` name (pinned on the
model), so an Alembic autogenerate sees no rename. Recreating a CHECK is a drop +
add: the new clause matches the model's rendering exactly (``IN`` list in model
order, single-quoted values).

Downgrade restores the 0015 three-value CHECK. Any ``unreadable`` rows present at
downgrade would violate that narrower CHECK, so the downgrade first remaps them to
``quarantined`` -- the verdict the sweep recorded for the same finding before this
change.

Revision ID: 0038_backup_health_unreadable
Revises: 0037_backup_only_when_running
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0038_backup_health_unreadable"
down_revision: str | None = "0037_backup_only_when_running"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_backup_health"
_NEW_CHECK = "health IN ('healthy', 'quarantined', 'unreadable', 'unknown')"
_OLD_CHECK = "health IN ('healthy', 'quarantined', 'unknown')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "backup", type_="check")
    op.create_check_constraint(_CONSTRAINT, "backup", _NEW_CHECK)


def downgrade() -> None:
    # Remap rows that only the widened CHECK admits before re-narrowing it, so the
    # ALTER never rejects data that was valid at head.
    op.execute("UPDATE backup SET health = 'quarantined' WHERE health = 'unreadable'")
    op.drop_constraint(_CONSTRAINT, "backup", type_="check")
    op.create_check_constraint(_CONSTRAINT, "backup", _OLD_CHECK)
