"""schedule_run: widen ``ck_schedule_run_outcome`` to ``unknown``

Issue #3660 adds a fourth ``ScheduleRunOutcome`` value, ``unknown``, for a
scheduled action the Worker did not answer in time. Such an action may still have
run, so recording it as ``failure`` was wrong. The live CHECK must admit the new
value or the runner's write would violate ``ck_schedule_run_outcome``. This
migration recreates the CHECK with the four values the model now renders.

No backfill: a ``failure`` row written before this change may have been a timeout
or a refusal, and its ``detail`` (``worker unavailable``) does not tell them apart,
so existing rows stay as they are.

The constraint keeps the SAME explicit ``ck_schedule_run_outcome`` name (pinned on
the model), so an Alembic autogenerate sees no rename. Recreating a CHECK is a drop
+ add: the new clause matches the model's rendering exactly (``IN`` list in model
order, single-quoted values).

Downgrade restores the 0029 three-value CHECK. Any ``unknown`` rows present at
downgrade would violate that narrower CHECK, so the downgrade first remaps them to
``failure``, which is what the runner recorded for the same run before this change.

Revision ID: 0044_schedule_run_unknown
Revises: 0043_worker_drain
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0044_schedule_run_unknown"
down_revision: str | None = "0043_worker_drain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_schedule_run_outcome"
_NEW_CHECK = "outcome IN ('success', 'failure', 'skipped', 'unknown')"
_OLD_CHECK = "outcome IN ('success', 'failure', 'skipped')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "schedule_run", type_="check")
    op.create_check_constraint(_CONSTRAINT, "schedule_run", _NEW_CHECK)


def downgrade() -> None:
    # Remap rows that only the widened CHECK admits before re-narrowing it, so the
    # ALTER never rejects data that was valid at head.
    op.execute("UPDATE schedule_run SET outcome = 'failure' WHERE outcome = 'unknown'")
    op.drop_constraint(_CONSTRAINT, "schedule_run", type_="check")
    op.create_check_constraint(_CONSTRAINT, "schedule_run", _OLD_CHECK)
