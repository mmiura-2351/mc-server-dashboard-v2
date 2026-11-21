"""Observable servers repository contracts shared by fakes and PostgreSQL.

Each case uses only a domain Port and a transaction boundary. The two runners
provide fresh repositories and the foreign-key parents needed by PostgreSQL.
"""

from __future__ import annotations

import datetime as dt
import inspect
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
    MAX_STATUS_DETAIL_BYTES,
    CommunityId,
    CrashReason,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
    bound_status_detail,
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
        crash_reason=CrashReason.FORGE_INSTALL_FAILED,
        crash_detail="forge install failed after 3 attempts",
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
        changed.awaiting_hydrate = True
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
        assert stored.awaiting_hydrate is False

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
                changed,
                expected_from=DesiredState.RUNNING,
                expected_assigned_worker_id=item.assigned_worker_id,
                write_config=True,
            )
            await tx.commit()
        changed.config["properties"]["motd"] = "changed-locally"
        async with server_repository_harness.open() as tx:
            assert not await tx.repository.update_lifecycle(
                changed,
                expected_from=DesiredState.RUNNING,
                expected_assigned_worker_id=item.assigned_worker_id,
                write_config=True,
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

    @pytest.mark.parametrize(
        ("stored", "expected", "applies"),
        [
            ("w1", "w1", True),
            (None, None, True),
            ("w1", None, False),
            (None, "w1", False),
            ("w1", "w2", False),
        ],
        ids=["same", "both-null", "null-expected", "null-stored", "other-worker"],
    )
    async def test_lifecycle_update_compares_the_assignment_it_read(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        stored: str | None,
        expected: str | None,
        applies: bool,
    ) -> None:
        # Issue #3792: a write whose read is stale on the assignment alone loses
        # as one stale on the desired state does, and leaves the row untouched.
        workers = {"w1": WorkerId(uuid.uuid4()), "w2": WorkerId(uuid.uuid4())}
        item = server(contract_community_id)
        item.assigned_worker_id = None if stored is None else workers[stored]
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        changed = deepcopy(item)
        changed.desired_state = DesiredState.STOPPED
        changed.updated_at = _LATER
        async with server_repository_harness.open() as tx:
            applied = await tx.repository.update_lifecycle(
                changed,
                expected_from=DesiredState.RUNNING,
                expected_assigned_worker_id=(
                    None if expected is None else workers[expected]
                ),
            )
            await tx.commit()
        assert applied is applies
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.desired_state, row.assigned_worker_id, row.updated_at) == (
            DesiredState.STOPPED if applies else DesiredState.RUNNING,
            item.assigned_worker_id,
            _LATER if applies else _NOW,
        )

    async def test_lifecycle_update_leaves_the_config_unless_it_owns_it(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
    ) -> None:
        # Only a start writes the config, to record the JAR it resolved (issue
        # #118). Every other transition keeps the row's config, so an edit that
        # committed after the caller read the row survives (issue #3792).
        item = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        changed = deepcopy(item)
        changed.desired_state = DesiredState.STOPPED
        changed.config = {"properties": {"motd": "read before an edit"}}
        changed.updated_at = _LATER
        async with server_repository_harness.open() as tx:
            assert await tx.repository.update_lifecycle(
                changed,
                expected_from=DesiredState.RUNNING,
                expected_assigned_worker_id=item.assigned_worker_id,
            )
            await tx.commit()
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.desired_state, row.config) == (DesiredState.STOPPED, item.config)

    async def test_lifecycle_update_has_no_default_for_the_expected_assignment(
        self, server_repository_harness: RepositoryHarness[ServerRepository]
    ) -> None:
        # Issue #3792: a caller cannot leave the assignment out of the compare.
        async with server_repository_harness.open() as tx:
            adapter = inspect.signature(tx.repository.update_lifecycle)
        port = inspect.signature(ServerRepository.update_lifecycle)
        for signature in (adapter, port):
            parameter = signature.parameters["expected_assigned_worker_id"]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is inspect.Parameter.empty

    @pytest.mark.parametrize(
        ("desired", "stored_worker", "stored_at", "read_at", "applies"),
        [
            (DesiredState.STOPPED, "w1", _NOW, _NOW, True),
            (DesiredState.STOPPED, "w1", _LATER, _NOW, False),
            (DesiredState.STOPPED, "w1", None, None, True),
            (DesiredState.STOPPED, "w1", _NOW, None, False),
            (DesiredState.RUNNING, "w1", _NOW, _NOW, False),
            (DesiredState.STOPPED, "w2", _NOW, _NOW, False),
        ],
        ids=[
            "as-read",
            "reported-since",
            "never-observed",
            "first-report-since",
            "running",
            "other-worker",
        ],
    )
    async def test_stale_assignment_clear_needs_the_row_as_it_was_read(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        desired: DesiredState,
        stored_worker: str,
        stored_at: dt.datetime | None,
        read_at: dt.datetime | None,
        applies: bool,
    ) -> None:
        # Issue #3766: the release a stop convergence owes a wedged row matches only
        # while the row is still desired=stopped on the Worker it read, and no
        # Worker report has landed since the read, a first report included.
        workers = {"w1": WorkerId(uuid.uuid4()), "w2": WorkerId(uuid.uuid4())}
        item = server(contract_community_id)
        item.desired_state = desired
        item.observed_state = ObservedState.STOPPED
        item.observed_at = stored_at
        item.assigned_worker_id = workers[stored_worker]
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            cleared = await tx.repository.clear_stale_assignment(
                item.id, workers["w1"], observed_at=read_at
            )
            await tx.commit()
        assert cleared is applies
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.desired_state, row.observed_at, row.assigned_worker_id) == (
            desired,
            stored_at,
            None if applies else item.assigned_worker_id,
        )

    @pytest.mark.parametrize(
        ("desired", "applies"),
        [(DesiredState.STOPPED, True), (DesiredState.RUNNING, False)],
        ids=["stopped", "running"],
    )
    async def test_an_observed_write_releases_only_a_stopped_intent(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        desired: DesiredState,
        applies: bool,
    ) -> None:
        # Issue #3766: an observed write that also clears the assignment is a
        # release, and like every other release it needs desired=stopped. Otherwise
        # the whole write is dropped, the observed state with it.
        item = server(contract_community_id)
        item.desired_state = desired
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            written = await tx.repository.record_observed_state(
                item.id,
                ObservedState.UNKNOWN,
                _LATER,
                unassign=True,
                expected_worker=item.assigned_worker_id,
            )
            await tx.commit()
        assert written is applies
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.observed_state, row.observed_at, row.assigned_worker_id) == (
            (ObservedState.UNKNOWN, _LATER, None)
            if applies
            else (ObservedState.CRASHED, _NOW, item.assigned_worker_id)
        )

    @pytest.mark.parametrize(
        ("detail", "reason", "stored"),
        [
            (
                "installer ran out of memory",
                CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
                (
                    CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
                    "installer ran out of memory",
                ),
            ),
            # An unclassified crash replaces the earlier crash's reason.
            (
                "container exited unexpectedly",
                None,
                (None, "container exited unexpectedly"),
            ),
            ("", None, (None, None)),
            (
                "x" * (MAX_STATUS_DETAIL_BYTES + 1),
                None,
                (None, bound_status_detail("x" * (MAX_STATUS_DETAIL_BYTES + 1))),
            ),
        ],
        ids=["classified", "unclassified", "bare", "over-bound"],
    )
    async def test_a_crash_report_stores_the_last_crash(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        detail: str,
        reason: CrashReason | None,
        stored: tuple[CrashReason | None, str | None],
    ) -> None:
        # Issue #3281: the reason and detail of the last crash are stored by the
        # write that records it, so a later read can explain the crash.
        item = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            written = await tx.repository.record_observed_state(
                item.id,
                ObservedState.CRASHED,
                _LATER,
                expected_worker=item.assigned_worker_id,
                detail=detail,
                reason=reason,
            )
            await tx.commit()
        assert written
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.crash_reason, row.crash_detail) == stored

    @pytest.mark.parametrize(
        "state",
        [
            ObservedState.STARTING,
            ObservedState.RUNNING,
            ObservedState.STOPPED,
            ObservedState.UNKNOWN,
        ],
        ids=lambda state: state.value,
    )
    async def test_leaving_crashed_clears_the_last_crash(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        state: ObservedState,
    ) -> None:
        # Issue #3281: the stored crash explains the current crash only, so the
        # write that moves the state off crashed drops it, the detail with it.
        item = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            assert await tx.repository.record_observed_state(
                item.id,
                state,
                _LATER,
                expected_worker=item.assigned_worker_id,
                detail="a detail of the new state",
            )
            await tx.commit()
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.observed_state, row.crash_reason, row.crash_detail) == (
            state,
            None,
            None,
        )

    @pytest.mark.parametrize(
        ("desired", "stamped"),
        [(DesiredState.RUNNING, _NOW), (DesiredState.STOPPED, _LATER)],
        ids=["running-intent", "stopped-intent"],
    )
    async def test_a_worker_disconnect_keeps_the_last_crash(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        desired: DesiredState,
        stamped: dt.datetime,
    ) -> None:
        # Issue #3885: the Worker has already forgotten a crashed instance, so
        # its disconnect tells nothing new about it, and the row keeps its crash.
        # Under a running intent it keeps its stamp too, as the API-startup reset
        # does: a newer stamp would hold back a restart that a later write has
        # re-armed (``Server.crash_not_restarted``). Under a stopped intent the
        # stamp moves to the disconnect, so the release waits a full grace from
        # it, for the Worker to come back and take the final snapshot. Another
        # Worker's crash is not this disconnect's: its stamp stays.
        item = server(contract_community_id)
        assert item.assigned_worker_id is not None
        item.desired_state = desired
        elsewhere = server(contract_community_id, name="elsewhere")
        assert elsewhere.assigned_worker_id != item.assigned_worker_id
        elsewhere.desired_state = desired
        elsewhere.game_port = 25566
        elsewhere.bedrock_port = 19133
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.repository.add(elsewhere)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            await tx.repository.mark_worker_servers_unknown(
                item.assigned_worker_id, _LATER
            )
            await tx.commit()
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
            other = await tx.repository.get_by_id(elsewhere.id)
        assert other is not None
        assert (other.observed_state, other.observed_at) == (
            ObservedState.CRASHED,
            _NOW,
        )
        assert row is not None
        assert (
            row.observed_state,
            row.observed_at,
            row.crash_reason,
            row.crash_detail,
        ) == (
            ObservedState.CRASHED,
            stamped,
            CrashReason.FORGE_INSTALL_FAILED,
            "forge install failed after 3 attempts",
        )

    @pytest.mark.parametrize(
        "state",
        [state for state in ObservedState if state is not ObservedState.CRASHED],
        ids=lambda state: state.value,
    )
    async def test_a_worker_disconnect_marks_any_other_state_unknown(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        state: ObservedState,
    ) -> None:
        # Only a crash survives the disconnect (issue #3885): every other state
        # of the Worker's servers is invalidated to unknown, at the new stamp.
        item = server(contract_community_id)
        assert item.assigned_worker_id is not None
        item.observed_state = state
        item.crash_reason = None
        item.crash_detail = None
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            await tx.repository.mark_worker_servers_unknown(
                item.assigned_worker_id, _LATER
            )
            await tx.commit()
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.observed_state, row.observed_at) == (ObservedState.UNKNOWN, _LATER)

    @pytest.mark.parametrize(
        ("state", "reason", "worker", "at"),
        [
            # The ownership guard (issue #1708): a report from a Worker the
            # server is not assigned to neither stores a crash nor clears one.
            (
                ObservedState.CRASHED,
                CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
                "other",
                _LATER,
            ),
            (ObservedState.RUNNING, None, "other", _LATER),
            # The monotonic guard (issue #216): a stale write changes nothing.
            (
                ObservedState.CRASHED,
                CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
                "own",
                _NOW,
            ),
            (ObservedState.RUNNING, None, "own", _NOW),
        ],
        ids=[
            "non-owner-crash",
            "non-owner-running",
            "stale-crash",
            "stale-running",
        ],
    )
    async def test_a_dropped_write_leaves_the_last_crash(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        state: ObservedState,
        reason: CrashReason | None,
        worker: str,
        at: dt.datetime,
    ) -> None:
        item = server(contract_community_id)
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            written = await tx.repository.record_observed_state(
                item.id,
                state,
                at,
                expected_worker=(
                    item.assigned_worker_id
                    if worker == "own"
                    else WorkerId(uuid.uuid4())
                ),
                detail="dropped",
                reason=reason,
            )
            await tx.commit()
        assert not written
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.observed_state, row.crash_reason, row.crash_detail) == (
            ObservedState.CRASHED,
            item.crash_reason,
            item.crash_detail,
        )

    async def test_a_placement_owes_its_hydrate_and_its_undo_owes_none(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
    ) -> None:
        # Issue #3890: the write that places a server on a Worker marks the
        # hydrate that Worker owes, and the write that undoes the placement
        # clears it with the assignment.
        worker = WorkerId(uuid.uuid4())
        item = server(contract_community_id)
        item.desired_state = DesiredState.STOPPED
        item.assigned_worker_id = None
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        placed = deepcopy(item)
        placed.desired_state = DesiredState.RUNNING
        placed.assigned_worker_id = worker
        async with server_repository_harness.open() as tx:
            assert await tx.repository.update_lifecycle(
                placed,
                expected_from=DesiredState.STOPPED,
                expected_assigned_worker_id=None,
            )
            await tx.commit()
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.assigned_worker_id, row.awaiting_hydrate) == (worker, True)

        undone = deepcopy(placed)
        undone.desired_state = DesiredState.STOPPED
        undone.assigned_worker_id = None
        async with server_repository_harness.open() as tx:
            assert await tx.repository.update_lifecycle(
                undone,
                expected_from=DesiredState.RUNNING,
                expected_assigned_worker_id=worker,
            )
            await tx.commit()
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.assigned_worker_id, row.awaiting_hydrate) == (None, False)

    @pytest.mark.parametrize("stored_mark", [True, False], ids=["owed", "cleared"])
    async def test_a_lifecycle_update_that_keeps_the_worker_keeps_the_mark(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        stored_mark: bool,
    ) -> None:
        # Issue #3890: a stop, drain or restart read the row before a hydrate
        # moved the mark. Writing back what it read would undo that hydrate.
        item = server(contract_community_id)
        item.awaiting_hydrate = stored_mark
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        stale = deepcopy(item)
        stale.awaiting_hydrate = not stored_mark
        stale.desired_state = DesiredState.STOPPED
        async with server_repository_harness.open() as tx:
            assert await tx.repository.update_lifecycle(
                stale,
                expected_from=DesiredState.RUNNING,
                expected_assigned_worker_id=item.assigned_worker_id,
            )
            await tx.commit()
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.desired_state, row.awaiting_hydrate) == (
            DesiredState.STOPPED,
            stored_mark,
        )

    @pytest.mark.parametrize(
        ("holder", "read", "stored_mark", "applies"),
        [
            ("w1", "as-stored", True, True),
            ("w1", "as-stored", False, False),
            ("w2", "as-stored", True, False),
            (None, "as-stored", True, False),
            ("w1", "older", True, False),
        ],
        ids=["clear", "already-clear", "other-worker", "unassigned", "re-placed"],
    )
    async def test_the_mark_is_cleared_only_for_the_placement_read(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        holder: str | None,
        read: str,
        stored_mark: bool,
        applies: bool,
    ) -> None:
        # Issue #3890: a start clears the mark once worker w1 holds the store's
        # working set. The write asserts the row as the start read it, so it never
        # touches another Worker's placement, nor a newer placement on w1, whose
        # write moved ``updated_at`` (r2 review).
        workers = {"w1": WorkerId(uuid.uuid4()), "w2": WorkerId(uuid.uuid4())}
        item = server(contract_community_id)
        item.assigned_worker_id = None if holder is None else workers[holder]
        item.awaiting_hydrate = stored_mark
        item.updated_at = _LATER
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            changed = await tx.repository.clear_awaiting_hydrate(
                item.id,
                workers["w1"],
                updated_at=_LATER if read == "as-stored" else _NOW,
            )
            await tx.commit()
        assert changed is applies
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert row.awaiting_hydrate is (False if applies else stored_mark)

    @pytest.mark.parametrize(
        ("holder", "stored_mark", "applies"),
        [
            ("w1", False, True),
            ("w1", True, False),
            ("w2", False, False),
            (None, False, False),
        ],
        ids=["mark", "already-marked", "other-worker", "unassigned"],
    )
    async def test_the_mark_is_set_again_only_while_the_worker_holds_the_row(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        holder: str | None,
        stored_mark: bool,
        applies: bool,
    ) -> None:
        # Issue #3890: a skipped hydrate the Worker disproves owes it again (#2499).
        workers = {"w1": WorkerId(uuid.uuid4()), "w2": WorkerId(uuid.uuid4())}
        item = server(contract_community_id)
        item.assigned_worker_id = None if holder is None else workers[holder]
        item.awaiting_hydrate = stored_mark
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            changed = await tx.repository.mark_awaiting_hydrate(item.id, workers["w1"])
            await tx.commit()
        assert changed is applies
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert row.awaiting_hydrate is (True if applies else stored_mark)

    @pytest.mark.parametrize(
        ("desired", "state", "unassign", "observed_at", "mark_after"),
        [
            (DesiredState.RUNNING, ObservedState.RUNNING, False, _LATER, False),
            (DesiredState.RUNNING, ObservedState.UNKNOWN, False, _LATER, True),
            (DesiredState.RUNNING, ObservedState.STARTING, False, _LATER, True),
            (DesiredState.RUNNING, ObservedState.RUNNING, False, _NOW, True),
            (DesiredState.STOPPED, ObservedState.STOPPED, True, _LATER, False),
            (DesiredState.STOPPED, ObservedState.STOPPED, False, _LATER, True),
        ],
        ids=[
            "running",
            "unknown",
            "starting",
            "dropped-running",
            "release",
            "stopped-kept",
        ],
    )
    async def test_a_running_report_or_a_release_clears_the_mark(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        desired: DesiredState,
        state: ObservedState,
        unassign: bool,
        observed_at: dt.datetime,
        mark_after: bool,
    ) -> None:
        # Issue #3890: a running instance's working set is what its snapshots
        # capture, and a release owes no hydrate. Only a write that lands counts.
        item = server(contract_community_id)
        item.desired_state = desired
        item.awaiting_hydrate = True
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            await tx.repository.record_observed_state(
                item.id,
                state,
                observed_at,
                unassign=unassign,
                expected_worker=item.assigned_worker_id,
            )
            await tx.commit()
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert row.awaiting_hydrate is mark_after

    @pytest.mark.parametrize("release", ["final-snapshot", "stale"])
    async def test_a_stop_release_clears_the_mark(
        self,
        server_repository_harness: RepositoryHarness[ServerRepository],
        contract_community_id: CommunityId,
        release: str,
    ) -> None:
        # Issue #3890: the mark goes with the assignment it was about.
        item = server(contract_community_id)
        item.desired_state = DesiredState.STOPPED
        item.awaiting_hydrate = True
        worker = item.assigned_worker_id
        assert worker is not None
        async with server_repository_harness.open() as tx:
            await tx.repository.add(item)
            await tx.commit()
        async with server_repository_harness.open() as tx:
            if release == "final-snapshot":
                cleared = await tx.repository.clear_assignment_after_final_snapshot(
                    item.id, worker
                )
            else:
                cleared = await tx.repository.clear_stale_assignment(
                    item.id, worker, observed_at=item.observed_at
                )
            await tx.commit()
        assert cleared
        async with server_repository_harness.open() as tx:
            row = await tx.repository.get_by_id(item.id)
        assert row is not None
        assert (row.assigned_worker_id, row.awaiting_hydrate) == (None, False)

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

    async def test_pending_sync_is_marked_once_per_kind_and_cleared(
        self,
        group_repository_harness: RepositoryHarness[GroupRepository],
        contract_server_id: ServerId,
    ) -> None:
        async with group_repository_harness.open() as tx:
            await tx.repository.mark_sync_pending(
                [contract_server_id], GroupKind.WHITELIST
            )
            await tx.repository.mark_sync_pending([contract_server_id], GroupKind.OP)
            await tx.repository.mark_sync_pending([contract_server_id], GroupKind.OP)
            await tx.commit()
        async with group_repository_harness.open() as tx:
            pending = await tx.repository.list_sync_pending(contract_server_id)
            other = await tx.repository.list_sync_pending(ServerId(uuid.uuid4()))
        assert [p.kind for p in pending] == [GroupKind.OP, GroupKind.WHITELIST]
        assert other == []
        async with group_repository_harness.open() as tx:
            await tx.repository.clear_sync_pending(contract_server_id, pending)
            await tx.commit()
        async with group_repository_harness.open() as tx:
            assert await tx.repository.list_sync_pending(contract_server_id) == []

    async def test_clear_leaves_a_mark_recorded_again_since_it_was_read(
        self,
        group_repository_harness: RepositoryHarness[GroupRepository],
        contract_server_id: ServerId,
    ) -> None:
        async with group_repository_harness.open() as tx:
            await tx.repository.mark_sync_pending([contract_server_id], GroupKind.OP)
            await tx.commit()
        async with group_repository_harness.open() as tx:
            read = await tx.repository.list_sync_pending(contract_server_id)
        async with group_repository_harness.open() as tx:
            await tx.repository.mark_sync_pending([contract_server_id], GroupKind.OP)
            await tx.commit()
        async with group_repository_harness.open() as tx:
            await tx.repository.clear_sync_pending(contract_server_id, read)
            await tx.commit()
        async with group_repository_harness.open() as tx:
            remaining = await tx.repository.list_sync_pending(contract_server_id)
        assert [p.kind for p in remaining] == [GroupKind.OP]
        assert remaining != read


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
