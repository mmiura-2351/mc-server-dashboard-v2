"""server: a community holding a server cannot be deleted

Issue #3218: ``fk_server_community_id_community`` was ``ON DELETE CASCADE``, so
deleting a community deleted its ``server`` rows, and through them the backup
rows, with none of what a server deletion does: no at-rest check, no lifecycle
lock, no stop, no storage retention. A running server lost its control record
while its process kept running, and its archives and working set stayed in
storage with no row left to reach them through.

The foreign key becomes ``ON DELETE RESTRICT``. A community's servers are
deleted first, each through the server deletion; the community deletion is
refused while one remains. The same foreign key serializes that check with a
concurrent server creation: the creation's INSERT holds the community row
``FOR KEY SHARE`` until it commits, which the deletion waits for.

No data changes: the referential condition is the same, only the action on
delete differs. Downgrade restores ``ON DELETE CASCADE``.

Revision ID: 0042_server_community_restrict
Revises: 0041_server_group_sync_pending
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0042_server_community_restrict"
down_revision: str | None = "0041_server_group_sync_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK = "fk_server_community_id_community"


def _recreate(ondelete: str) -> None:
    op.drop_constraint(_FK, "server", type_="foreignkey")
    op.create_foreign_key(
        _FK, "server", "community", ["community_id"], ["id"], ondelete=ondelete
    )


def upgrade() -> None:
    _recreate("RESTRICT")


def downgrade() -> None:
    _recreate("CASCADE")
