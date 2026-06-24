"""Use-case tests for player groups against in-memory fakes (issue #276).

Covers CRUD, the per-community/kind name-uniqueness rule, cross-community
not-found, player upsert/remove, attach/detach, and the file-sync posture: an
at-rest attached server gets its ops.json / whitelist.json regenerated (exact MC
schema, union-merge by uuid), while a running server is left pending.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest

from mc_server_dashboard_api.servers.application.groups import (
    AddPlayer,
    AttachGroup,
    CreateGroup,
    DeleteGroup,
    DetachGroup,
    ListGroups,
    ListGroupServers,
    ListServerGroups,
    ReadGroup,
    RemovePlayer,
    RenameGroup,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    GroupAttachmentNotFoundError,
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    InvalidGroupKindError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.groups import (
    DEFAULT_OP_LEVEL,
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import FakeFileStore, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 5, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())
_OTHER_COMMUNITY = CommunityId(uuid.uuid4())


def _server(
    *,
    community: CommunityId = _COMMUNITY,
    desired: DesiredState = DesiredState.STOPPED,
    observed: ObservedState = ObservedState.STOPPED,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community,
        name=ServerName("srv"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _seed_group(
    uow: FakeUnitOfWork,
    *,
    community: CommunityId = _COMMUNITY,
    kind: GroupKind = GroupKind.OP,
    players: list[Player] | None = None,
) -> PlayerGroup:
    group = PlayerGroup(
        id=GroupId.new(),
        community_id=community,
        name=GroupName("admins"),
        kind=kind,
        players=players or [],
    )
    uow.groups.seed(group)
    return group


async def test_create_group_persists_and_commits() -> None:
    uow = FakeUnitOfWork()
    group = await CreateGroup(uow=uow)(community_id=_COMMUNITY, name="ops", kind="op")
    assert group.kind is GroupKind.OP
    assert uow.commits == 1
    assert await uow.groups.get_by_id(group.id) is not None


async def test_create_group_rejects_unknown_kind() -> None:
    with pytest.raises(InvalidGroupKindError):
        await CreateGroup(uow=FakeUnitOfWork())(
            community_id=_COMMUNITY, name="ops", kind="banned"
        )


async def test_create_group_rejects_duplicate_name_within_kind() -> None:
    uow = FakeUnitOfWork()
    _seed_group(uow, kind=GroupKind.OP)
    with pytest.raises(GroupNameAlreadyExistsError):
        await CreateGroup(uow=uow)(community_id=_COMMUNITY, name="admins", kind="op")


async def test_same_name_different_kind_is_allowed() -> None:
    uow = FakeUnitOfWork()
    _seed_group(uow, kind=GroupKind.OP)
    group = await CreateGroup(uow=uow)(
        community_id=_COMMUNITY, name="admins", kind="whitelist"
    )
    assert group.kind is GroupKind.WHITELIST


async def test_read_group_cross_community_is_not_found() -> None:
    uow = FakeUnitOfWork()
    group = _seed_group(uow, community=_OTHER_COMMUNITY)
    with pytest.raises(GroupNotFoundError):
        await ReadGroup(uow=uow)(community_id=_COMMUNITY, group_id=group.id)


async def test_list_groups_scopes_to_community() -> None:
    uow = FakeUnitOfWork()
    _seed_group(uow, community=_COMMUNITY)
    _seed_group(uow, community=_OTHER_COMMUNITY)
    groups = await ListGroups(uow=uow)(community_id=_COMMUNITY)
    assert len(groups) == 1


async def test_rename_group_rejects_clashing_name() -> None:
    uow = FakeUnitOfWork()
    a = _seed_group(uow, kind=GroupKind.OP)
    # Second group of the same kind named "mods".
    b = PlayerGroup(
        id=GroupId.new(),
        community_id=_COMMUNITY,
        name=GroupName("mods"),
        kind=GroupKind.OP,
        players=[],
    )
    uow.groups.seed(b)
    with pytest.raises(GroupNameAlreadyExistsError):
        await RenameGroup(uow=uow)(
            community_id=_COMMUNITY, group_id=b.id, name="admins"
        )
    assert a.name.value == "admins"


async def test_add_player_upserts_username() -> None:
    uow = FakeUnitOfWork()
    group = _seed_group(uow)
    pid = uuid.uuid4()
    await AddPlayer(uow=uow, file_store=FakeFileStore())(
        community_id=_COMMUNITY, group_id=group.id, player_uuid=pid, username="alice"
    )
    updated = await AddPlayer(uow=uow, file_store=FakeFileStore())(
        community_id=_COMMUNITY, group_id=group.id, player_uuid=pid, username="alice2"
    )
    assert [(p.uuid, p.username) for p in updated.players] == [(pid, "alice2")]


async def test_remove_player() -> None:
    uow = FakeUnitOfWork()
    pid = uuid.uuid4()
    group = _seed_group(uow, players=[Player(pid, "alice")])
    updated = await RemovePlayer(uow=uow, file_store=FakeFileStore())(
        community_id=_COMMUNITY, group_id=group.id, player_uuid=pid
    )
    assert updated.players == []


async def test_attach_requires_existing_server_in_community() -> None:
    uow = FakeUnitOfWork()
    group = _seed_group(uow)
    foreign = _server(community=_OTHER_COMMUNITY)
    uow.servers.seed(foreign)
    with pytest.raises(ServerNotFoundError):
        await AttachGroup(uow=uow, file_store=FakeFileStore())(
            community_id=_COMMUNITY, group_id=group.id, server_id=foreign.id
        )


async def test_detach_unattached_is_not_found() -> None:
    uow = FakeUnitOfWork()
    group = _seed_group(uow)
    server = _server()
    uow.servers.seed(server)
    with pytest.raises(GroupAttachmentNotFoundError):
        await DetachGroup(uow=uow, file_store=FakeFileStore())(
            community_id=_COMMUNITY, group_id=group.id, server_id=server.id
        )


async def test_attach_op_group_writes_ops_json_for_at_rest_server() -> None:
    uow = FakeUnitOfWork()
    pid = uuid.uuid4()
    group = _seed_group(uow, kind=GroupKind.OP, players=[Player(pid, "alice")])
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    await AttachGroup(uow=uow, file_store=fs)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )
    assert "ops.json" in fs.files
    written = json.loads(fs.files["ops.json"])
    assert written == [
        {
            "uuid": str(pid),
            "name": "alice",
            "level": DEFAULT_OP_LEVEL,
            "bypassesPlayerLimit": False,
        }
    ]


async def test_attach_whitelist_group_writes_whitelist_json() -> None:
    uow = FakeUnitOfWork()
    pid = uuid.uuid4()
    group = _seed_group(uow, kind=GroupKind.WHITELIST, players=[Player(pid, "bob")])
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    await AttachGroup(uow=uow, file_store=fs)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )
    assert json.loads(fs.files["whitelist.json"]) == [{"uuid": str(pid), "name": "bob"}]


async def test_running_server_is_left_pending() -> None:
    uow = FakeUnitOfWork()
    group = _seed_group(uow, kind=GroupKind.OP, players=[Player(uuid.uuid4(), "a")])
    server = _server(desired=DesiredState.RUNNING, observed=ObservedState.RUNNING)
    uow.servers.seed(server)
    fs = FakeFileStore()
    await AttachGroup(uow=uow, file_store=fs)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )
    # No at-rest write: the running server picks up the copy on its next hydrate.
    assert fs.files == {}


async def test_two_attached_op_groups_union_merge_by_uuid() -> None:
    uow = FakeUnitOfWork()
    u1 = uuid.UUID("00000000-0000-0000-0000-000000000002")
    u2 = uuid.UUID("00000000-0000-0000-0000-000000000001")
    g1 = _seed_group(uow, kind=GroupKind.OP, players=[Player(u1, "two")])
    g2 = PlayerGroup(
        id=GroupId.new(),
        community_id=_COMMUNITY,
        name=GroupName("mods"),
        kind=GroupKind.OP,
        players=[Player(u2, "one"), Player(u1, "dup")],
    )
    uow.groups.seed(g2)
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    await AttachGroup(uow=uow, file_store=fs)(
        community_id=_COMMUNITY, group_id=g1.id, server_id=server.id
    )
    await AttachGroup(uow=uow, file_store=fs)(
        community_id=_COMMUNITY, group_id=g2.id, server_id=server.id
    )
    entries = json.loads(fs.files["ops.json"])
    # Deduplicated by uuid (u1 appears once despite being in both groups), sorted
    # by uuid string (u2 < u1). The username on the deduped uuid is whichever
    # group sorts first — deterministic given the by-id group order, not asserted
    # here (either "two" or "dup" is acceptable; first-wins is documented).
    assert [e["uuid"] for e in entries] == [str(u2), str(u1)]


async def test_remove_player_resyncs_attached_server() -> None:
    uow = FakeUnitOfWork()
    pid = uuid.uuid4()
    group = _seed_group(uow, kind=GroupKind.OP, players=[Player(pid, "alice")])
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    await AttachGroup(uow=uow, file_store=fs)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )
    await RemovePlayer(uow=uow, file_store=fs)(
        community_id=_COMMUNITY, group_id=group.id, player_uuid=pid
    )
    assert json.loads(fs.files["ops.json"]) == []


async def test_delete_group_resyncs_previously_attached_server() -> None:
    uow = FakeUnitOfWork()
    pid = uuid.uuid4()
    group = _seed_group(uow, kind=GroupKind.OP, players=[Player(pid, "alice")])
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    await AttachGroup(uow=uow, file_store=fs)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )
    await DeleteGroup(uow=uow, file_store=fs)(
        community_id=_COMMUNITY, group_id=group.id
    )
    assert json.loads(fs.files["ops.json"]) == []
    assert await uow.groups.get_by_id(group.id) is None


class _MiddleFailFileStore(FakeFileStore):
    """File store that raises only for one server id, recording per-server writes.

    Drives the multi-server partial-failure posture: the middle server's write
    fails while the other two succeed.
    """

    def __init__(self, fail_for: ServerId) -> None:
        super().__init__()
        self._fail_for = fail_for
        self.written_servers: list[ServerId] = []

    async def write_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        if server_id == self._fail_for:
            raise RuntimeError("forced storage write failure")
        self.written_servers.append(server_id)


async def test_add_player_continues_past_one_failing_server(
    caplog: pytest.LogCaptureFixture,
) -> None:
    uow = FakeUnitOfWork()
    group = _seed_group(uow, kind=GroupKind.OP)
    # Three at-rest servers; the sync visits them sorted by uuid string, so the
    # middle id by that order is the one whose write fails.
    servers = [_server() for _ in range(3)]
    for server in servers:
        uow.servers.seed(server)
        await uow.groups.attach(group.id, server.id)
    middle = sorted(servers, key=lambda s: str(s.id.value))[1]
    fs = _MiddleFailFileStore(fail_for=middle.id)

    with caplog.at_level("WARNING"):
        await AddPlayer(uow=uow, file_store=fs)(
            community_id=_COMMUNITY,
            group_id=group.id,
            player_uuid=uuid.uuid4(),
            username="alice",
        )

    # The other two servers were synced; no raise despite the middle failure.
    synced = {s.value for s in fs.written_servers}
    assert synced == {s.id.value for s in servers if s.id != middle.id}
    # Exactly one WARN names the failed server and the group.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "server_id") == str(middle.id.value)
    assert getattr(warnings[0], "group_id") == str(group.id.value)


async def test_list_server_groups_and_group_servers() -> None:
    uow = FakeUnitOfWork()
    group = _seed_group(uow)
    server = _server()
    uow.servers.seed(server)
    await AttachGroup(uow=uow, file_store=FakeFileStore())(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )
    server_groups = await ListServerGroups(uow=uow)(
        community_id=_COMMUNITY, server_id=server.id
    )
    group_servers = await ListGroupServers(uow=uow)(
        community_id=_COMMUNITY, group_id=group.id
    )
    assert [g.id for g in server_groups] == [group.id]
    assert group_servers == [server.id]
