"""Observable servers repository contracts shared by fakes and PostgreSQL.

Each case uses only a domain Port and a transaction boundary. The two runners
provide fresh repositories and the foreign-key parents needed by PostgreSQL.
"""

from __future__ import annotations

import datetime as dt
import uuid
from copy import deepcopy

import pytest
from sqlalchemy.exc import IntegrityError

from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.backup_repository import BackupRepository
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    PluginAlreadyExistsError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
)
from mc_server_dashboard_api.servers.domain.group_repository import GroupRepository
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.plugin_repository import PluginRepository
from mc_server_dashboard_api.servers.domain.repositories import ServerRepository
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.resource_pack_repository import (
    ResourcePackRepository,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    ScheduleRunId,
    ScheduleRunOutcome,
)
from mc_server_dashboard_api.servers.domain.schedule_repository import (
    ScheduleRepository,
    ScheduleRunRepository,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.contracts.repository import RepositoryHarness

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.UTC)
_LATER = _NOW + dt.timedelta(minutes=5)


def server(community_id: CommunityId, *, name: str = "srv") -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community_id,
        name=ServerName(name),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={"properties": {"motd": "original"}},
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.CRASHED,
        observed_at=_NOW,
        assigned_worker_id=WorkerId(uuid.uuid4()),
        created_at=_NOW,
        updated_at=_NOW,
        game_port=25565,
        bedrock_port=19132,
        slug=f"srv-{uuid.uuid4().hex[:12]}",
        backup_retention={"nested": {"keep": 2}},
    )


def backup(server_id: ServerId) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref="archive/ref",
        size_bytes=10,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


def plugin(server_id: ServerId) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path="mods/a.jar",
        filename="a.jar",
        display_name="A",
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.MODRINTH,
        source_project_id="project-a",
        source_version_id="version-a",
        version_number="1.0",
        checksum_sha512="a" * 128,
        sha256="b" * 64,
        size_bytes=1,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        provides=["alias"],
        dependencies=[{"mod_identifier": "dep", "required": True}],
        mc_versions=["1.21.1"],
        catalog_dependencies=[{"project_id": "cdep", "required": True}],
    )


def group(community_id: CommunityId) -> PlayerGroup:
    return PlayerGroup(
        id=GroupId.new(),
        community_id=community_id,
        name=GroupName("ops"),
        kind=GroupKind.OP,
        players=[Player(uuid.uuid4(), "steve")],
    )


def schedule(server_id: ServerId) -> Schedule:
    return Schedule(
        id=ScheduleId.new(),
        server_id=server_id,
        name="nightly",
        action=ScheduleAction.BACKUP,
        cadence=Cadence.from_cron("0 4 * * *"),
        enabled=True,
        next_run_at=_LATER,
        created_at=_NOW,
        updated_at=_NOW,
    )


def run(schedule_id: ScheduleId) -> ScheduleRun:
    return ScheduleRun(
        id=ScheduleRunId.new(),
        schedule_id=schedule_id,
        started_at=_NOW,
        finished_at=_NOW + dt.timedelta(seconds=1),
        outcome=ScheduleRunOutcome.SUCCESS,
        detail=None,
    )


def pack() -> ResourcePack:
    return ResourcePack(
        id=ResourcePackId.new(),
        filename="pack.zip",
        display_name="Pack",
        description=None,
        sha1_hash="a" * 40,
        sha256_hash="b" * 64,
        size_bytes=128,
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def assignment(server_id: ServerId, pack_id: ResourcePackId) -> ResourcePackAssignment:
    return ResourcePackAssignment(
        server_id=server_id,
        resource_pack_id=pack_id,
        require_resource_pack=True,
        resource_pack_prompt=None,
        assigned_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


class ServerRepositoryContract:
    async def test_add_detaches_after_commit(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
    ) -> None:
        item = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()

        item.name = ServerName("changed-locally")
        item.config["properties"]["motd"] = "changed-locally"
        assert item.backup_retention is not None
        item.backup_retention["nested"]["keep"] = 99
        async with server_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(item.id)
        assert stored is not None
        assert stored.name == ServerName("srv")
        assert stored.config == {"properties": {"motd": "original"}}
        assert stored.backup_retention == {"nested": {"keep": 2}}

    @pytest.mark.parametrize(
        "reader",
        [
            "get_by_id",
            "get_by_community_and_name",
            "get_by_slug",
            "list_for_community",
            "list_all",
            "list_desired_running_assigned",
            "list_reconcilable",
        ],
    )
    async def test_readers_do_not_write_through(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        reader: str,
    ) -> None:
        item = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            repo = tx.repository
            if reader == "get_by_id":
                loaded = await repo.get_by_id(item.id)
            elif reader == "get_by_community_and_name":
                loaded = await repo.get_by_community_and_name(
                    item.community_id, item.name
                )
            elif reader == "get_by_slug":
                loaded = await repo.get_by_slug(item.slug)
            elif reader == "list_for_community":
                loaded = next(
                    row
                    for row in await repo.list_for_community(item.community_id)
                    if row.id == item.id
                )
            elif reader == "list_all":
                loaded = next(row for row in await repo.list_all() if row.id == item.id)
            elif reader == "list_desired_running_assigned":
                loaded = next(
                    row
                    for row in await repo.list_desired_running_assigned()
                    if row.id == item.id
                )
            else:
                loaded = next(
                    row for row in await repo.list_reconcilable() if row.id == item.id
                )
        assert loaded is not None
        loaded.name = ServerName("changed-locally")
        loaded.config["properties"]["motd"] = "changed-locally"
        assert loaded.backup_retention is not None
        loaded.backup_retention["nested"]["keep"] = 99
        async with server_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(item.id)
        assert stored is not None
        assert stored.name == ServerName("srv")
        assert stored.config == {"properties": {"motd": "original"}}
        assert stored.backup_retention == {"nested": {"keep": 2}}

    async def test_update_writes_only_edit_fields_and_detaches_them(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
    ) -> None:
        item = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        changed = deepcopy(item)
        changed.name = ServerName("renamed")
        changed.config = {"properties": {"motd": "edited"}}
        changed.game_port = 25566
        changed.bedrock_port = 19133
        changed.slug = f"renamed-{uuid.uuid4().hex[:12]}"
        changed.updated_at = _LATER
        changed.desired_state = DesiredState.STOPPED
        changed.observed_state = ObservedState.UNKNOWN
        changed.observed_at = _LATER
        changed.assigned_worker_id = None
        changed.backup_retention = {"nested": {"keep": 7}}
        async with server_repository_harness.open() as tx:
            await tx.repository.update(changed)
            await tx.commit()
        changed.config["properties"]["motd"] = "changed-locally"
        async with server_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(item.id)
        assert stored is not None
        assert (stored.name, stored.config, stored.game_port, stored.bedrock_port) == (
            ServerName("renamed"),
            {"properties": {"motd": "edited"}},
            25566,
            19133,
        )
        assert stored.slug == changed.slug
        assert stored.updated_at == _LATER
        assert (stored.desired_state, stored.observed_state, stored.observed_at) == (
            DesiredState.RUNNING,
            ObservedState.CRASHED,
            _NOW,
        )
        assert stored.assigned_worker_id == item.assigned_worker_id
        assert stored.backup_retention == {"nested": {"keep": 2}}

    async def test_lifecycle_update_is_guarded_and_writes_only_lifecycle_fields(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
    ) -> None:
        item = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        changed = deepcopy(item)
        changed.desired_state = DesiredState.STOPPED
        changed.assigned_worker_id = None
        changed.config = {"properties": {"motd": "resolved"}}
        changed.updated_at = _LATER
        changed.name = ServerName("stale-name")
        changed.observed_state = ObservedState.UNKNOWN
        changed.observed_at = _LATER
        async with server_repository_harness.open() as tx:
            assert await tx.repository.update_lifecycle(
                changed, expected_from=DesiredState.RUNNING
            )
            await tx.commit()
        changed.config["properties"]["motd"] = "changed-locally"
        async with server_repository_harness.open() as tx:
            assert not await tx.repository.update_lifecycle(
                changed, expected_from=DesiredState.RUNNING
            )
            stored = await tx.repository.get_by_id(item.id)
        assert stored is not None
        assert (stored.desired_state, stored.assigned_worker_id, stored.config) == (
            DesiredState.STOPPED,
            None,
            {"properties": {"motd": "resolved"}},
        )
        assert stored.updated_at == _LATER
        assert (stored.name, stored.observed_state, stored.observed_at) == (
            ServerName("srv"),
            ObservedState.CRASHED,
            _NOW,
        )

    async def test_missing_updates_do_not_insert(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
    ) -> None:
        missing = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.update(missing)
            await tx.repository.update_backup_retention(missing.id, {"keep_last": 3})
            await tx.commit()
        async with server_repository_harness.open() as tx:
            assert await tx.repository.get_by_id(missing.id) is None

    async def test_backup_retention_writer_detaches_nested_values(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
    ) -> None:
        item = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        retention = {"nested": {"keep": 5}}
        async with server_repository_harness.open() as tx:
            await tx.repository.update_backup_retention(item.id, retention)
            await tx.commit()
        retention["nested"]["keep"] = 99
        async with server_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(item.id)
        assert stored is not None
        assert stored.backup_retention == {"nested": {"keep": 5}}


class BackupRepositoryContract:
    async def test_add_and_readers_detach_metadata(
        self,
        backup_repository_harness: RepositoryHarness[BackupRepository],
        contract_server_id: ServerId,
    ) -> None:
        item = backup(contract_server_id)
        async with backup_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        item.storage_ref = "changed-locally"
        async with backup_repository_harness.open() as tx:
            loaded = await tx.repository.get_by_id(item.id)
            listed = await tx.repository.list_for_server(contract_server_id)
        assert loaded is not None
        assert len(listed) == 1
        assert loaded.storage_ref == listed[0].storage_ref == "archive/ref"
        loaded.storage_ref = "changed-by-get"
        listed[0].health = BackupHealth.QUARANTINED
        async with backup_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(item.id)
        assert stored is not None
        assert (stored.storage_ref, stored.health) == (
            "archive/ref",
            BackupHealth.HEALTHY,
        )


class PluginRepositoryContract:
    async def test_add_and_readers_detach_nested_metadata(
        self,
        plugin_repository_harness: RepositoryHarness[PluginRepository],
        contract_server_id: ServerId,
    ) -> None:
        item = plugin(contract_server_id)
        async with plugin_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        item.dependencies[0]["mod_identifier"] = "changed-locally"
        item.catalog_dependencies[0]["project_id"] = "changed-locally"
        async with plugin_repository_harness.open() as tx:
            repo = tx.repository
            loaded = [
                await repo.get_by_id(item.server_id, item.id),
                await repo.get_by_rel_path(item.server_id, item.rel_path),
                await repo.get_by_source_project_id(item.server_id, "project-a"),
                (await repo.list_for_server(item.server_id))[0],
                (await repo.list_catalog_plugins(item.server_id))[0],
            ]
        for entity in loaded:
            assert entity is not None
            assert entity.dependencies == [{"mod_identifier": "dep", "required": True}]
            assert entity.catalog_dependencies == [
                {"project_id": "cdep", "required": True}
            ]
            entity.display_name = "changed-by-reader"
            entity.provides.append("changed-by-reader")
            entity.mc_versions.append("changed-by-reader")
            entity.dependencies[0]["mod_identifier"] = "changed-by-reader"
            entity.catalog_dependencies[0]["project_id"] = "changed-by-reader"
        async with plugin_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(item.server_id, item.id)
        assert stored is not None
        assert (stored.display_name, stored.provides, stored.mc_versions) == (
            "A",
            ["alias"],
            ["1.21.1"],
        )
        assert stored.dependencies == [{"mod_identifier": "dep", "required": True}]
        assert stored.catalog_dependencies == [{"project_id": "cdep", "required": True}]

    async def test_update_detaches_nested_metadata_and_missing_update_is_noop(
        self,
        plugin_repository_harness: RepositoryHarness[PluginRepository],
        contract_server_id: ServerId,
    ) -> None:
        item = plugin(contract_server_id)
        async with plugin_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        changed = deepcopy(item)
        changed.display_name = "Updated"
        changed.dependencies[0]["mod_identifier"] = "updated-dep"
        async with plugin_repository_harness.open() as tx:
            await tx.repository.update(changed)
            await tx.commit()
        changed.display_name = "changed-locally"
        changed.dependencies[0]["mod_identifier"] = "changed-locally"
        missing = plugin(contract_server_id)
        async with plugin_repository_harness.open() as tx:
            await tx.repository.update(missing)
            await tx.commit()
        async with plugin_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(item.server_id, item.id)
            absent = await tx.repository.get_by_id(missing.server_id, missing.id)
        assert stored is not None
        assert (stored.display_name, stored.dependencies) == (
            "Updated",
            [{"mod_identifier": "updated-dep", "required": True}],
        )
        assert absent is None

    async def test_update_cannot_move_onto_an_occupied_plugin_path(
        self,
        plugin_repository_harness: RepositoryHarness[PluginRepository],
        contract_server_id: ServerId,
    ) -> None:
        moving = plugin(contract_server_id)
        occupied = plugin(contract_server_id)
        occupied.rel_path = "mods/occupied.jar"
        async with plugin_repository_harness.open() as tx:
            await tx.repository.add(moving)
            await tx.repository.add(occupied)
            await tx.commit()
        moving.rel_path = occupied.rel_path
        async with plugin_repository_harness.open() as tx:
            with pytest.raises(PluginAlreadyExistsError):
                await tx.repository.update(moving)
        async with plugin_repository_harness.open() as tx:
            saved = await tx.repository.get_by_id(contract_server_id, moving.id)
        assert saved is not None and saved.rel_path == "mods/a.jar"


class GroupRepositoryContract:
    async def test_add_save_and_readers_detach_players(
        self,
        group_repository_harness: RepositoryHarness[GroupRepository],
        contract_community_id: CommunityId,
    ) -> None:
        item = group(contract_community_id)
        async with group_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        item.name = GroupName("changed-locally")
        item.players.append(Player(uuid.uuid4(), "changed-locally"))
        async with group_repository_harness.open() as tx:
            repo = tx.repository
            loaded = await repo.get_by_id(item.id)
            named = await repo.get_by_community_kind_name(
                contract_community_id, GroupKind.OP, GroupName("ops")
            )
            listed = (await repo.list_for_community(contract_community_id))[0]
        assert loaded is not None and named is not None
        for entity in (loaded, named, listed):
            assert entity.name == GroupName("ops")
            assert [p.username for p in entity.players] == ["steve"]
            entity.players.append(Player(uuid.uuid4(), "changed-by-reader"))
        async with group_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(item.id)
        assert stored is not None
        assert [p.username for p in stored.players] == ["steve"]
        stored.name = GroupName("renamed")
        stored.players.append(Player(uuid.uuid4(), "alex"))
        async with group_repository_harness.open() as tx:
            await tx.repository.save(stored)
            await tx.commit()
        stored.players.clear()
        async with group_repository_harness.open() as tx:
            saved = await tx.repository.get_by_id(item.id)
        assert saved is not None
        assert saved.name == GroupName("renamed")
        assert {p.username for p in saved.players} == {"steve", "alex"}

    @pytest.mark.parametrize("with_players", [True, False])
    async def test_save_missing_group_reports_not_found(
        self,
        group_repository_harness: RepositoryHarness[GroupRepository],
        contract_community_id: CommunityId,
        with_players: bool,
    ) -> None:
        missing = group(contract_community_id)
        if not with_players:
            missing.players.clear()
        async with group_repository_harness.open() as tx:
            with pytest.raises(GroupNotFoundError):
                await tx.repository.save(missing)
        async with group_repository_harness.open() as tx:
            assert await tx.repository.get_by_id(missing.id) is None

    async def test_attached_groups_are_read_as_detached_entities(
        self,
        group_repository_harness: RepositoryHarness[GroupRepository],
        contract_community_id: CommunityId,
        contract_server_id: ServerId,
    ) -> None:
        item = group(contract_community_id)
        async with group_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.repository.attach(item.id, contract_server_id)
            await tx.commit()
        async with group_repository_harness.open() as tx:
            all_groups = await tx.repository.list_groups_for_server(contract_server_id)
            kind_groups = await tx.repository.list_groups_for_server_kind(
                contract_server_id, GroupKind.OP
            )
        assert [g.id for g in all_groups] == [item.id]
        assert [g.id for g in kind_groups] == [item.id]
        all_groups[0].players.clear()
        kind_groups[0].name = GroupName("changed-locally")
        async with group_repository_harness.open() as tx:
            saved = await tx.repository.get_by_id(item.id)
        assert saved is not None
        assert saved.name == GroupName("ops")
        assert [p.username for p in saved.players] == ["steve"]

    async def test_names_are_unique_within_community_and_kind(
        self,
        group_repository_harness: RepositoryHarness[GroupRepository],
        contract_community_id: CommunityId,
    ) -> None:
        first = group(contract_community_id)
        second = group(contract_community_id)
        second.name = GroupName("other")
        async with group_repository_harness.open() as tx:
            await tx.repository.add(first)
            await tx.repository.add(second)
            await tx.commit()
        duplicate = group(contract_community_id)
        async with group_repository_harness.open() as tx:
            with pytest.raises(GroupNameAlreadyExistsError):
                await tx.repository.add(duplicate)
        second.name = GroupName("ops")
        async with group_repository_harness.open() as tx:
            with pytest.raises(GroupNameAlreadyExistsError):
                await tx.repository.save(second)
        async with group_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(second.id)
        assert stored is not None and stored.name == GroupName("other")

    async def test_add_with_an_existing_id_does_not_replace_the_group(
        self,
        group_repository_harness: RepositoryHarness[GroupRepository],
        contract_community_id: CommunityId,
    ) -> None:
        item = group(contract_community_id)
        async with group_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        duplicate = deepcopy(item)
        duplicate.name = GroupName("replacement")

        async with group_repository_harness.open() as tx:
            with pytest.raises(IntegrityError):
                await tx.repository.add(duplicate)
        async with group_repository_harness.open() as tx:
            stored = await tx.repository.get_by_id(item.id)
        assert stored is not None and stored.name == GroupName("ops")

    async def test_attach_missing_group_reports_not_found(
        self,
        group_repository_harness: RepositoryHarness[GroupRepository],
        contract_server_id: ServerId,
    ) -> None:
        missing_id = GroupId.new()
        async with group_repository_harness.open() as tx:
            with pytest.raises(GroupNotFoundError):
                await tx.repository.attach(missing_id, contract_server_id)
        async with group_repository_harness.open() as tx:
            assert not await tx.repository.is_attached(missing_id, contract_server_id)


class ScheduleRepositoryContract:
    async def test_add_update_and_readers_detach_rows(
        self,
        schedule_repository_harness: RepositoryHarness[ScheduleRepository],
        contract_server_id: ServerId,
    ) -> None:
        item = schedule(contract_server_id)
        async with schedule_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        item.name = "changed-locally"
        async with schedule_repository_harness.open() as tx:
            loaded = await tx.repository.get_by_id(item.id)
            listed = (await tx.repository.list_for_server(contract_server_id))[0]
        assert loaded is not None
        assert loaded.name == listed.name == "nightly"
        loaded.name = "changed-by-reader"
        listed.name = "changed-by-list"
        async with schedule_repository_harness.open() as tx:
            saved = await tx.repository.get_by_id(item.id)
        assert saved is not None and saved.name == "nightly"
        saved.name = "weekly"
        saved.updated_at = _LATER
        async with schedule_repository_harness.open() as tx:
            await tx.repository.update(saved)
            await tx.commit()
        saved.name = "changed-locally"
        async with schedule_repository_harness.open() as tx:
            updated = await tx.repository.get_by_id(item.id)
        assert updated is not None
        assert (updated.name, updated.updated_at) == ("weekly", _LATER)

    async def test_update_missing_schedule_does_not_insert(
        self,
        schedule_repository_harness: RepositoryHarness[ScheduleRepository],
        contract_server_id: ServerId,
    ) -> None:
        missing = schedule(contract_server_id)
        async with schedule_repository_harness.open() as tx:
            await tx.repository.update(missing)
            await tx.commit()
        async with schedule_repository_harness.open() as tx:
            assert await tx.repository.get_by_id(missing.id) is None


class ScheduleRunRepositoryContract:
    async def test_add_and_list_detach_run_rows(
        self,
        schedule_run_repository_harness: RepositoryHarness[ScheduleRunRepository],
        contract_schedule_id: ScheduleId,
    ) -> None:
        item = run(contract_schedule_id)
        async with schedule_run_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        item.detail = "changed-locally"
        async with schedule_run_repository_harness.open() as tx:
            listed = await tx.repository.list_for_schedule(contract_schedule_id)
        assert len(listed) == 1
        assert listed[0].detail is None
        listed[0].outcome = ScheduleRunOutcome.FAILURE
        async with schedule_run_repository_harness.open() as tx:
            saved = await tx.repository.list_for_schedule(contract_schedule_id)
        assert len(saved) == 1
        assert saved[0].outcome is ScheduleRunOutcome.SUCCESS


class ResourcePackRepositoryContract:
    async def test_add_and_readers_detach_pack(
        self,
        resource_pack_repository_harness: RepositoryHarness[ResourcePackRepository],
    ) -> None:
        item = pack()
        async with resource_pack_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        item.display_name = "changed-locally"
        async with resource_pack_repository_harness.open() as tx:
            loaded = await tx.repository.get_by_id(item.id)
            listed = (await tx.repository.list_all())[0]
        assert loaded is not None
        assert loaded.display_name == listed.display_name == "Pack"
        loaded.display_name = "changed-by-get"
        listed.display_name = "changed-by-list"
        async with resource_pack_repository_harness.open() as tx:
            saved = await tx.repository.get_by_id(item.id)
        assert saved is not None and saved.display_name == "Pack"

    async def test_assignment_add_and_readers_detach_metadata(
        self,
        resource_pack_repository_harness: RepositoryHarness[ResourcePackRepository],
        contract_server_id: ServerId,
    ) -> None:
        item = pack()
        link = assignment(contract_server_id, item.id)
        async with resource_pack_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with resource_pack_repository_harness.open() as tx:
            await tx.repository.add_assignment(link)
            await tx.commit()
        link.require_resource_pack = False
        async with resource_pack_repository_harness.open() as tx:
            loaded = await tx.repository.get_assignment_by_server(contract_server_id)
            listed = (await tx.repository.list_assignments_for_pack(item.id))[0]
        assert loaded is not None
        assert loaded.require_resource_pack and listed.require_resource_pack
        loaded.require_resource_pack = False
        listed.resource_pack_prompt = "changed-locally"
        async with resource_pack_repository_harness.open() as tx:
            saved = await tx.repository.get_assignment_by_server(contract_server_id)
        assert saved is not None
        assert saved.require_resource_pack
        assert saved.resource_pack_prompt is None

    async def test_pack_in_use_cannot_be_deleted(
        self,
        resource_pack_repository_harness: RepositoryHarness[ResourcePackRepository],
        contract_server_id: ServerId,
    ) -> None:
        item = pack()
        async with resource_pack_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with resource_pack_repository_harness.open() as tx:
            await tx.repository.add_assignment(assignment(contract_server_id, item.id))
            await tx.commit()
        async with resource_pack_repository_harness.open() as tx:
            with pytest.raises(ResourcePackInUseError):
                await tx.repository.delete(item.id)
        async with resource_pack_repository_harness.open() as tx:
            assert await tx.repository.get_by_id(item.id) is not None

    async def test_assignment_to_missing_pack_reports_not_found(
        self,
        resource_pack_repository_harness: RepositoryHarness[ResourcePackRepository],
        contract_server_id: ServerId,
    ) -> None:
        missing_id = ResourcePackId.new()
        async with resource_pack_repository_harness.open() as tx:
            with pytest.raises(ResourcePackNotFoundError):
                await tx.repository.add_assignment(
                    assignment(contract_server_id, missing_id)
                )
        async with resource_pack_repository_harness.open() as tx:
            assert (
                await tx.repository.get_assignment_by_server(contract_server_id) is None
            )

    async def test_second_assignment_for_one_server_is_refused(
        self,
        resource_pack_repository_harness: RepositoryHarness[ResourcePackRepository],
        contract_server_id: ServerId,
    ) -> None:
        item = pack()
        first = assignment(contract_server_id, item.id)
        async with resource_pack_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with resource_pack_repository_harness.open() as tx:
            await tx.repository.add_assignment(first)
            await tx.commit()

        async with resource_pack_repository_harness.open() as tx:
            with pytest.raises(IntegrityError):
                await tx.repository.add_assignment(
                    assignment(contract_server_id, item.id)
                )
        async with resource_pack_repository_harness.open() as tx:
            stored = await tx.repository.get_assignment_by_server(contract_server_id)
        assert stored is not None and stored.assigned_by == first.assigned_by
