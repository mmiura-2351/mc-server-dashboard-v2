"""baseline (empty)

The initial revision. Establishes the migration chain with no schema; entity
tables land in later revisions with their features (DATABASE.md).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
