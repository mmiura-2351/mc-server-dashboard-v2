"""Add missing indexes on join tables

Adds single-column indexes for the non-leading FK columns that hot queries
filter on but the existing composite PK / single-column PK cannot serve:

- ``server_group.server_id`` — used by ``list_groups_for_server(_kind)``
- ``server_resource_pack_assignments.resource_pack_id`` — used by
  ``list_assignments_for_pack``

Without these, both queries fall back to a sequential scan as the join tables
grow (issue #1621).

Revision ID: 0028_join_table_indexes
Revises: 0027_server_bedrock_port
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_join_table_indexes"
down_revision: str | None = "0027_server_bedrock_port"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_server_group_server_id",
        "server_group",
        ["server_id"],
    )
    op.create_index(
        "ix_srv_rp_assignments_resource_pack_id",
        "server_resource_pack_assignments",
        ["resource_pack_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_srv_rp_assignments_resource_pack_id",
        table_name="server_resource_pack_assignments",
    )
    op.drop_index(
        "ix_server_group_server_id",
        table_name="server_group",
    )
