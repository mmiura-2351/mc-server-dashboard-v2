"""schedule: backup-only ``only_when_running`` payload flag

Issue #2236 adds a per-schedule option on ``backup`` schedules to back up only
while the server is running: an occurrence that finds the server at rest records
a ``skipped`` run instead of archiving it. The flag is per-action payload, so it
lives in the ``payload`` jsonb beside ``command`` / ``warnings`` as
``{"only_when_running": <bool>}``, and it is held to backup rows by the new
``ck_schedule_backup_only_when_running`` CHECK: a backup row must carry a
boolean, every other row must not carry the key at all.

Owner decision on #2236: existing backup schedules take the new default (on),
not the old behavior, so the upgrade merges ``only_when_running: true`` into
every existing backup row's payload before adding the CHECK.

Downgrade drops the CHECK and strips the key, restoring the pre-0037 ``{}``
backup payload (which then backs up whether the server runs or not again).

Revision ID: 0037_backup_only_when_running
Revises: 0036_floodgate_slug_rewrite
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0037_backup_only_when_running"
down_revision: str | None = "0036_floodgate_slug_rewrite"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_schedule_backup_only_when_running"
# Matches the model's rendering in ``schedule_models``. ``payload -> key IS
# NULL`` means the key is absent (a JSON null is a non-NULL jsonb), and ``IS NOT
# DISTINCT FROM`` makes a missing key on a backup row a violation rather than an
# UNKNOWN that passes.
_CHECK = (
    "CASE WHEN action = 'backup' "
    "THEN jsonb_typeof(payload -> 'only_when_running') "
    "IS NOT DISTINCT FROM 'boolean' "
    "ELSE payload -> 'only_when_running' IS NULL END"
)


def upgrade() -> None:
    op.execute(
        "UPDATE schedule SET payload = payload || "
        "jsonb_build_object('only_when_running', true) "
        "WHERE action = 'backup'"
    )
    op.create_check_constraint(_CONSTRAINT, "schedule", _CHECK)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "schedule", type_="check")
    op.execute(
        "UPDATE schedule SET payload = payload - 'only_when_running' "
        "WHERE action = 'backup'"
    )
