"""Model-rendered DDL for the player-group tables (issue #276; DATABASE.md).

Verifies the three tables carry the documented constraints (the per-community/kind
name uniqueness, the kind CHECK enum, the upsert-key uniqueness, the cascade FKs)
so the model metadata matches the hand-written 0011 migration (the metadata-sync
integration test pins them to the migrated database).
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from mc_server_dashboard_api.servers.adapters.group_models import (
    GroupPlayerModel,
    PlayerGroupModel,
    ServerGroupModel,
)

_GROUP = cast(Table, PlayerGroupModel.__table__)
_PLAYER = cast(Table, GroupPlayerModel.__table__)
_SERVER_GROUP = cast(Table, ServerGroupModel.__table__)


def _unique_names(table: Table) -> set[str]:
    return {str(c.name) for c in table.constraints if isinstance(c, UniqueConstraint)}


def _check(table: Table, name: str) -> CheckConstraint:
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return constraint
    raise AssertionError(f"missing CHECK constraint {name!r}")


def test_player_group_unique_community_kind_name() -> None:
    assert "uq_player_group_community_kind_name" in _unique_names(_GROUP)


def test_player_group_kind_check_enum() -> None:
    sqltext = str(_check(_GROUP, "ck_player_group_kind").sqltext)
    assert "op" in sqltext
    assert "whitelist" in sqltext


def test_player_group_community_cascade() -> None:
    fks = list(_GROUP.c.community_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"


def test_group_player_unique_group_uuid() -> None:
    assert "uq_group_player_group_uuid" in _unique_names(_PLAYER)


def test_group_player_group_cascade() -> None:
    fks = list(_PLAYER.c.group_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"


def test_server_group_composite_pk_and_cascades() -> None:
    pk_columns = {str(c.name) for c in _SERVER_GROUP.primary_key.columns}
    assert pk_columns == {"group_id", "server_id"}
    for column in ("group_id", "server_id"):
        fks = list(_SERVER_GROUP.c[column].foreign_keys)
        assert len(fks) == 1
        assert fks[0].ondelete == "CASCADE"


def test_server_group_server_id_index() -> None:
    index_names = {idx.name for idx in _SERVER_GROUP.indexes}
    assert "ix_server_group_server_id" in index_names
