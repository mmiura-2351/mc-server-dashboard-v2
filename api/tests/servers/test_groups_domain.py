"""Unit tests for the player-group domain (issue #276).

Pins the pure rules: name/player validation, uuid-keyed upsert/remove, the
union-merge determinism across groups, and the exact ops.json / whitelist.json
render schemas.
"""

from __future__ import annotations

import uuid

import pytest

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidGroupNameError,
    InvalidPlayerError,
)
from mc_server_dashboard_api.servers.domain.groups import (
    DEFAULT_OP_LEVEL,
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
    merge_players,
    render_ops_json,
    render_whitelist_json,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId


def _group(kind: GroupKind, players: list[Player]) -> PlayerGroup:
    return PlayerGroup(
        id=GroupId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=GroupName("admins"),
        kind=kind,
        players=players,
    )


def test_group_name_trims_and_rejects_blank() -> None:
    assert GroupName("  ops  ").value == "ops"
    with pytest.raises(InvalidGroupNameError):
        GroupName("   ")


def test_player_rejects_blank_username() -> None:
    with pytest.raises(InvalidPlayerError):
        Player(uuid.uuid4(), "  ")


def test_upsert_player_adds_then_updates_username_by_uuid() -> None:
    pid = uuid.uuid4()
    group = _group(GroupKind.OP, [])
    group.upsert_player(Player(pid, "alice"))
    assert [p.username for p in group.players] == ["alice"]
    # Same uuid, new username -> replace, not append.
    group.upsert_player(Player(pid, "alice2"))
    assert [p.username for p in group.players] == ["alice2"]


def test_remove_player_reports_whether_removed() -> None:
    pid = uuid.uuid4()
    group = _group(GroupKind.OP, [Player(pid, "alice")])
    assert group.remove_player(pid) is True
    assert group.remove_player(pid) is False


def test_kind_target_file() -> None:
    assert GroupKind.OP.target_file == "ops.json"
    assert GroupKind.WHITELIST.target_file == "whitelist.json"


def test_merge_players_unions_and_sorts_by_uuid() -> None:
    u1 = uuid.UUID("00000000-0000-0000-0000-000000000002")
    u2 = uuid.UUID("00000000-0000-0000-0000-000000000001")
    g_a = _group(GroupKind.OP, [Player(u1, "two"), Player(u2, "one")])
    g_b = _group(GroupKind.OP, [Player(u2, "one-dup")])
    merged = merge_players([g_a, g_b])
    # Deduplicated by uuid, first occurrence wins, sorted by uuid string.
    assert [(str(p.uuid), p.username) for p in merged] == [
        (str(u2), "one"),
        (str(u1), "two"),
    ]


def test_merge_players_is_attach_order_independent() -> None:
    u1 = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    u2 = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
    g_a = _group(GroupKind.WHITELIST, [Player(u1, "a")])
    g_b = _group(GroupKind.WHITELIST, [Player(u2, "b")])
    assert merge_players([g_a, g_b]) == merge_players([g_b, g_a])


def test_render_ops_json_exact_schema() -> None:
    pid = uuid.uuid4()
    assert render_ops_json([Player(pid, "alice")]) == [
        {
            "uuid": str(pid),
            "name": "alice",
            "level": DEFAULT_OP_LEVEL,
            "bypassesPlayerLimit": False,
        }
    ]


def test_render_whitelist_json_exact_schema() -> None:
    pid = uuid.uuid4()
    assert render_whitelist_json([Player(pid, "bob")]) == [
        {"uuid": str(pid), "name": "bob"}
    ]
