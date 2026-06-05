"""SQLAlchemy ORM models for player groups (issue #276; DATABASE.md Section 7).

Three tables, normalized to match the existing relational model (DATABASE.md
Section 2):

- ``player_group`` — the community-scoped group of one ``kind`` (op / whitelist),
  ``UNIQUE(community_id, kind, name)`` and a ``kind`` CHECK enum.
- ``group_player`` — a player row under a group, ``UNIQUE(group_id, player_uuid)``
  (the upsert key); deleted with its group (``ON DELETE CASCADE``).
- ``server_group`` — the many-to-many attachment join (group <-> server), a
  composite PK; rows cascade when either side is deleted.

The framework-free domain entity is translated to/from these models in the
repository. The ``community_id`` / ``server_id`` FKs cascade so deleting a
community or a server tidies its groups/attachments automatically.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base

_GROUP_KINDS = ("op", "whitelist")


class PlayerGroupModel(Base):
    """Row of the ``player_group`` table (DATABASE.md Section 7, issue #276)."""

    __tablename__ = "player_group"
    __table_args__ = (
        UniqueConstraint(
            "community_id", "kind", "name", name="uq_player_group_community_kind_name"
        ),
        CheckConstraint(
            f"kind IN ({', '.join(repr(v) for v in _GROUP_KINDS)})",
            name="ck_player_group_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    community_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("community.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)


class GroupPlayerModel(Base):
    """Row of the ``group_player`` table (a player under a group, issue #276)."""

    __tablename__ = "group_player"
    __table_args__ = (
        UniqueConstraint("group_id", "player_uuid", name="uq_group_player_group_uuid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("player_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    player_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    username: Mapped[str] = mapped_column(String, nullable=False)


class ServerGroupModel(Base):
    """Row of the ``server_group`` attachment join (issue #276)."""

    __tablename__ = "server_group"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("player_group.id", ondelete="CASCADE"),
        primary_key=True,
    )
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("server.id", ondelete="CASCADE"),
        primary_key=True,
    )
