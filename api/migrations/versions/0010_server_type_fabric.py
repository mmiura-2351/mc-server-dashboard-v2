"""servers: widen ``ck_server_type`` to fabric and spigot

The 0005 migration pinned the ``server_type`` CHECK to
``('vanilla', 'paper', 'forge')``. Issue #264 widened the ORM model's
``_SERVER_TYPES`` (and the ``ServerType`` enum) to also accept ``fabric`` and
``spigot``, but the live CHECK was never altered -- so a ``fabric`` (or
``spigot``) INSERT on a migrated database violated ``ck_server_type``. This
migration recreates the CHECK with the five values the model now renders.

The constraint keeps the SAME explicit ``ck_server_type`` name (pinned on the
model since the #165 ck-prefix incident, so the name-sync test stays quiet and
an Alembic autogenerate sees no rename). Recreating a CHECK is a drop + add: the
new clause must match the model's rendering exactly (``IN`` list in model order,
single-quoted values).

Downgrade restores the 0005 three-value CHECK. Any ``fabric``/``spigot`` rows
present at downgrade would violate it; there are none in the documented flow
(spigot is rejected at create-time, fabric is new in this change).

Revision ID: 0010_server_type_fabric
Revises: 0009_server_game_port
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_server_type_fabric"
down_revision: str | None = "0009_server_game_port"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_server_type"
_NEW_CHECK = "server_type IN ('vanilla', 'paper', 'fabric', 'forge', 'spigot')"
_OLD_CHECK = "server_type IN ('vanilla', 'paper', 'forge')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "server", type_="check")
    op.create_check_constraint(_CONSTRAINT, "server", _NEW_CHECK)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "server", type_="check")
    op.create_check_constraint(_CONSTRAINT, "server", _OLD_CHECK)
