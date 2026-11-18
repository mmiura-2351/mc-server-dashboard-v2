"""server: store the last crash's reason and detail with the observed state

Issue #3281: a crash's free-text ``detail`` and its typed reason (issue #1093)
rode only the live status frame. A page loaded after the crash could not explain
it, and the reconciler could not tell a crash the Worker deliberately does not
retry from one it may restart.

``crash_reason`` and ``crash_detail`` are written by the observed-state write
that records ``crashed`` and set to NULL by the next write of any other state.
``crash_reason`` is NULL for an unclassified crash; its CHECK
(``ck_server_crash_reason``, pinned on the model) admits the reasons the
control-plane ``CrashReason`` names. ``crash_detail`` is the Worker's text,
bounded by the API before the write.

No backfill: neither value was stored before, so a server already crashed reads
NULL in both until its next crash.

Downgrade drops both columns, their CHECK with them.

Revision ID: 0045_server_last_crash
Revises: 0044_schedule_run_unknown
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_server_last_crash"
down_revision: str | None = "0044_schedule_run_unknown"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("server", sa.Column("crash_reason", sa.String(), nullable=True))
    op.add_column("server", sa.Column("crash_detail", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_server_crash_reason",
        "server",
        "crash_reason IN ('forge_install_failed', 'forge_install_out_of_memory', "
        "'forge_install_java_incompatible')",
    )


def downgrade() -> None:
    op.drop_column("server", "crash_detail")
    op.drop_column("server", "crash_reason")
