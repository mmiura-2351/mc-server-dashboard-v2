"""Use-case tests for server CRUD against in-memory fakes (TESTING.md Section 4).

Covers create (+ backend/type validation), community-scoped read/list,
cross-community not-found, update editability rules (backend immutable, at-rest
gate, name clash), and delete (at-rest gate + grant sweep atomicity).
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
    ListServers,
    ReadServer,
    UpdateServer,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ExecutionBackendImmutableError,
    InvalidBackupScheduleError,
    InvalidSnapshotIntervalError,
    PortAlreadyTakenError,
    PortOutOfRangeError,
    PortRangeExhaustedError,
    ServerFileNotFoundError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    ServerNotStoppedError,
    UnknownExecutionBackendError,
    UnknownServerTypeError,
    UnsupportedEditionError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.servers.domain.version_validator import (
    UnknownVersionError,
    UnsupportedServerTypeError,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeUnitOfWork,
    FakeVersionValidator,
)
from tests.storage.helpers import drain, read_tar

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_LATER = dt.datetime(2026, 6, 4, 13, 0, tzinfo=dt.timezone.utc)
_PORTS = PortRange(start=25565, end=25664)


def _server(
    *,
    community_id: CommunityId,
    name: str = "survival",
    desired: DesiredState = DesiredState.STOPPED,
    observed: ObservedState = ObservedState.STOPPED,
    backend: ExecutionBackend = ExecutionBackend.HOST_PROCESS,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community_id,
        name=ServerName(name),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=backend,
        config={"motd": "hi"},
        desired_state=desired,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


# --- create ----------------------------------------------------------------


async def test_create_defaults_to_stopped_and_commits() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=_PORTS,
    )(
        community_id=community,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="paper",
        execution_backend="container",
        config={"motd": "hi"},
    )
    assert server.desired_state is DesiredState.STOPPED
    assert server.observed_state is ObservedState.STOPPED
    assert server.observed_at is None
    assert server.assigned_worker_id is None
    assert server.server_type is ServerType.PAPER
    assert server.execution_backend is ExecutionBackend.CONTAINER
    assert uow.commits == 1
    assert uow.servers.by_id[server.id] is server


async def test_create_accepts_java_edition() -> None:
    uow = FakeUnitOfWork()
    validator = FakeVersionValidator()
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=validator,
        file_store=FakeFileStore(),
        port_range=_PORTS,
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )
    assert server.mc_edition == "java"
    assert uow.commits == 1


async def test_create_rejects_non_java_edition() -> None:
    uow = FakeUnitOfWork()
    validator = FakeVersionValidator()
    with pytest.raises(UnsupportedEditionError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=validator,
            file_store=FakeFileStore(),
            port_range=_PORTS,
        )(
            community_id=CommunityId(uuid.uuid4()),
            name="s",
            mc_edition="bedrock",
            mc_version="1.21.1",
            server_type="vanilla",
            execution_backend="host_process",
            config={},
        )
    # Rejected before staging or even consulting the catalog (Java-only at M1).
    assert uow.commits == 0
    assert validator.calls == []


async def test_create_rejects_unknown_server_type() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(UnknownServerTypeError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(),
            file_store=FakeFileStore(),
            port_range=_PORTS,
        )(
            community_id=CommunityId(uuid.uuid4()),
            name="s",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="bedrock-not-supported",
            execution_backend="host_process",
            config={},
        )
    assert uow.commits == 0


async def test_create_rejects_unknown_execution_backend() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(UnknownExecutionBackendError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(),
            file_store=FakeFileStore(),
            port_range=_PORTS,
        )(
            community_id=CommunityId(uuid.uuid4()),
            name="s",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            execution_backend="kubernetes",
            config={},
        )
    assert uow.commits == 0


async def test_create_rejects_unsupported_type_forge() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(UnsupportedServerTypeError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(unsupported={"forge"}),
            file_store=FakeFileStore(),
            port_range=_PORTS,
        )(
            community_id=CommunityId(uuid.uuid4()),
            name="s",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="forge",
            execution_backend="host_process",
            config={},
        )
    assert uow.commits == 0


async def test_create_rejects_unknown_version() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(UnknownVersionError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(offered={"vanilla": {"1.21.1"}}),
            file_store=FakeFileStore(),
            port_range=_PORTS,
        )(
            community_id=CommunityId(uuid.uuid4()),
            name="s",
            mc_edition="java",
            mc_version="9.9.9",
            server_type="vanilla",
            execution_backend="host_process",
            config={},
        )
    assert uow.commits == 0


async def test_create_with_accept_eula_seeds_eula_and_properties() -> None:
    # accept_eula=True composes eula.txt with the always-seeded server.properties
    # (port assignment, #243): both land in the initial working set, in order.
    uow = FakeUnitOfWork()
    file_store = FakeFileStore()
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
        accept_eula=True,
    )
    assert file_store.writes == [
        ("server.properties", b"server-port=25565\n"),
        ("eula.txt", b"eula=true\n"),
    ]
    assert file_store.files["eula.txt"] == b"eula=true\n"
    assert file_store.files["server.properties"] == b"server-port=25565\n"
    assert uow.commits == 1
    assert uow.servers.by_id[server.id] is server


async def test_create_without_accept_eula_still_seeds_properties() -> None:
    # Default (accept_eula omitted): server.properties is still seeded with the
    # assigned game port (#243), but no eula.txt (issue #198 unchanged).
    uow = FakeUnitOfWork()
    file_store = FakeFileStore()
    await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )
    assert file_store.writes == [("server.properties", b"server-port=25565\n")]
    assert "eula.txt" not in file_store.files


# --- create: game port assignment (#243) -----------------------------------


async def test_create_auto_assigns_lowest_free_port() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    # Seed two servers holding the two lowest ports; the next create gets 25567.
    taken_a = _server(community_id=community, name="a")
    taken_a.game_port = 25565
    taken_b = _server(community_id=community, name="b")
    taken_b.game_port = 25566
    uow.servers.seed(taken_a)
    uow.servers.seed(taken_b)
    file_store = FakeFileStore()
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
    )(
        community_id=community,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )
    assert server.game_port == 25567
    assert uow.servers.by_id[server.id].game_port == 25567
    assert file_store.writes == [("server.properties", b"server-port=25567\n")]


async def test_create_honors_explicit_free_port() -> None:
    uow = FakeUnitOfWork()
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=_PORTS,
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
        game_port=25600,
    )
    assert server.game_port == 25600


async def test_create_rejects_explicit_taken_port() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    taken = _server(community_id=community, name="a")
    taken.game_port = 25600
    uow.servers.seed(taken)
    with pytest.raises(PortAlreadyTakenError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(),
            file_store=FakeFileStore(),
            port_range=_PORTS,
        )(
            community_id=community,
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            execution_backend="host_process",
            config={},
            game_port=25600,
        )
    assert uow.commits == 0


async def test_create_rejects_explicit_out_of_range_port() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(PortOutOfRangeError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(),
            file_store=FakeFileStore(),
            port_range=_PORTS,
        )(
            community_id=CommunityId(uuid.uuid4()),
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            execution_backend="host_process",
            config={},
            game_port=80,
        )
    assert uow.commits == 0


async def test_create_raises_when_range_exhausted() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    # A one-port range that is already taken leaves nothing to auto-assign.
    taken = _server(community_id=community, name="a")
    taken.game_port = 25565
    uow.servers.seed(taken)
    with pytest.raises(PortRangeExhaustedError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(),
            file_store=FakeFileStore(),
            port_range=PortRange(start=25565, end=25565),
        )(
            community_id=community,
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            execution_backend="host_process",
            config={},
        )
    assert uow.commits == 0


async def test_create_seed_failure_surfaces_after_commit() -> None:
    # A storage failure during seeding leaves the committed row in place and raises
    # the mapped seed-failure error (issue #243 design comment).
    uow = FakeUnitOfWork()
    with pytest.raises(WorkingSetSeedFailedError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(),
            file_store=FakeFileStore(fail_write=True),
            port_range=_PORTS,
        )(
            community_id=CommunityId(uuid.uuid4()),
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            execution_backend="host_process",
            config={},
        )
    # The row committed before seeding; it is left in place (repairable).
    assert uow.commits == 1
    assert len(uow.servers.by_id) == 1


# --- read / list -----------------------------------------------------------


async def test_read_returns_server_in_its_community() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    got = await ReadServer(uow=uow)(community_id=community, server_id=server.id)
    assert got.id == server.id


async def test_read_other_communitys_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    community_a = CommunityId(uuid.uuid4())
    community_b = CommunityId(uuid.uuid4())
    server = _server(community_id=community_a)
    uow.servers.seed(server)
    with pytest.raises(ServerNotFoundError):
        await ReadServer(uow=uow)(community_id=community_b, server_id=server.id)


async def test_list_is_scoped_to_the_community() -> None:
    uow = FakeUnitOfWork()
    community_a = CommunityId(uuid.uuid4())
    community_b = CommunityId(uuid.uuid4())
    uow.servers.seed(_server(community_id=community_a, name="a1"))
    uow.servers.seed(_server(community_id=community_a, name="a2"))
    uow.servers.seed(_server(community_id=community_b, name="b1"))
    listed = await ListServers(uow=uow)(community_id=community_a)
    assert {s.name.value for s in listed} == {"a1", "a2"}


# --- update ----------------------------------------------------------------


async def test_update_edits_name_and_config_while_at_rest() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    updated = await UpdateServer(uow=uow, clock=FakeClock(_LATER))(
        community_id=community,
        server_id=server.id,
        name="creative",
        config={"motd": "bye"},
    )
    assert updated.name == ServerName("creative")
    assert updated.config == {"motd": "bye"}
    assert updated.updated_at == _LATER
    assert uow.commits == 1


async def test_update_accepts_snapshot_interval_override_at_or_above_floor() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    updated = await UpdateServer(
        uow=uow, clock=FakeClock(_LATER), min_interval_seconds=300
    )(
        community_id=community,
        server_id=server.id,
        config={"snapshot_interval_seconds": 600},
    )
    assert updated.config["snapshot_interval_seconds"] == 600
    assert uow.commits == 1


async def test_update_rejects_snapshot_interval_override_below_floor() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(InvalidSnapshotIntervalError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER), min_interval_seconds=300)(
            community_id=community,
            server_id=server.id,
            config={"snapshot_interval_seconds": 60},
        )
    assert uow.commits == 0


async def test_update_accepts_backup_schedule_override() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    updated = await UpdateServer(uow=uow, clock=FakeClock(_LATER))(
        community_id=community,
        server_id=server.id,
        config={"backup_interval_hours": 6},
    )
    assert updated.config["backup_interval_hours"] == 6
    assert uow.commits == 1


async def test_update_rejects_invalid_backup_schedule_override() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(InvalidBackupScheduleError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER))(
            community_id=community,
            server_id=server.id,
            config={"backup_interval_hours": 0},
        )
    assert uow.commits == 0


async def test_update_rejects_backend_change_as_immutable() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community, backend=ExecutionBackend.HOST_PROCESS)
    uow.servers.seed(server)
    with pytest.raises(ExecutionBackendImmutableError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER))(
            community_id=community,
            server_id=server.id,
            execution_backend="container",
        )
    assert uow.commits == 0


async def test_update_allows_same_backend_value() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community, backend=ExecutionBackend.HOST_PROCESS)
    uow.servers.seed(server)
    updated = await UpdateServer(uow=uow, clock=FakeClock(_LATER))(
        community_id=community,
        server_id=server.id,
        execution_backend="host_process",
        config={"k": "v"},
    )
    assert updated.config == {"k": "v"}


async def test_update_rejects_while_running() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    with pytest.raises(ServerNotStoppedError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER))(
            community_id=community,
            server_id=server.id,
            name="creative",
        )
    assert uow.commits == 0


async def test_update_safe_key_only_succeeds_while_running() -> None:
    # Cadence knobs (snapshot_interval_seconds, backup_interval_hours) are
    # operationally safe: a change touching only them bypasses the at-rest gate
    # (issue #115). The incoming config must preserve the existing unsafe keys.
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    updated = await UpdateServer(
        uow=uow, clock=FakeClock(_LATER), min_interval_seconds=300
    )(
        community_id=community,
        server_id=server.id,
        config={"motd": "hi", "snapshot_interval_seconds": 600},
    )
    assert updated.config["snapshot_interval_seconds"] == 600
    assert uow.commits == 1


async def test_update_unsafe_key_rejected_while_running() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    with pytest.raises(ServerNotStoppedError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER))(
            community_id=community,
            server_id=server.id,
            config={"motd": "changed"},
        )
    assert uow.commits == 0


async def test_update_mixed_safe_and_unsafe_keys_rejected_while_running() -> None:
    # A safe-key change carried alongside any unsafe-key change keeps the at-rest
    # requirement: the whole update is gated (issue #115).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    with pytest.raises(ServerNotStoppedError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER), min_interval_seconds=300)(
            community_id=community,
            server_id=server.id,
            config={"motd": "changed", "snapshot_interval_seconds": 600},
        )
    assert uow.commits == 0


async def test_update_below_floor_while_running_validates_before_state() -> None:
    # Precedence: validation errors (below the thrash floor) are raised before the
    # state gate, so a running server with a below-floor override 422s, not 409s
    # (issue #115 second ask).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    with pytest.raises(InvalidSnapshotIntervalError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER), min_interval_seconds=300)(
            community_id=community,
            server_id=server.id,
            config={"motd": "hi", "snapshot_interval_seconds": 60},
        )
    assert uow.commits == 0


async def test_update_removing_unsafe_key_rejected_while_running() -> None:
    # Dropping an existing unsafe key counts as touching it: the at-rest gate
    # still applies even though the only key sent is a safe one (issue #115).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    with pytest.raises(ServerNotStoppedError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER), min_interval_seconds=300)(
            community_id=community,
            server_id=server.id,
            config={"snapshot_interval_seconds": 600},
        )
    assert uow.commits == 0


async def test_update_adding_null_unsafe_key_rejected_while_running() -> None:
    # Adding an unsafe key with a JSON null value is a key-PRESENCE change, even
    # though both sides .get() to None. It must keep the at-rest gate so a null
    # add cannot be smuggled past on a running server (issue #115).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    with pytest.raises(ServerNotStoppedError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER), min_interval_seconds=300)(
            community_id=community,
            server_id=server.id,
            config={
                "motd": "hi",
                "feature_flag": None,
                "snapshot_interval_seconds": 600,
            },
        )
    assert uow.commits == 0


async def test_update_removing_null_unsafe_key_rejected_while_running() -> None:
    # Removing an existing null-valued unsafe key is a key-PRESENCE change even
    # though both sides .get() to None. It must keep the at-rest gate (issue #115).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    server.config = {"motd": "hi", "feature_flag": None}
    uow.servers.seed(server)
    with pytest.raises(ServerNotStoppedError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER), min_interval_seconds=300)(
            community_id=community,
            server_id=server.id,
            config={"motd": "hi", "snapshot_interval_seconds": 600},
        )
    assert uow.commits == 0


async def test_update_rejects_name_clash_in_community() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    uow.servers.seed(_server(community_id=community, name="taken"))
    target = _server(community_id=community, name="survival")
    uow.servers.seed(target)
    with pytest.raises(ServerNameAlreadyExistsError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER))(
            community_id=community,
            server_id=target.id,
            name="taken",
        )


async def test_update_other_communitys_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server(community_id=CommunityId(uuid.uuid4()))
    uow.servers.seed(server)
    with pytest.raises(ServerNotFoundError):
        await UpdateServer(uow=uow, clock=FakeClock(_LATER))(
            community_id=CommunityId(uuid.uuid4()),
            server_id=server.id,
            name="x",
        )


# --- delete ----------------------------------------------------------------


async def test_delete_removes_server_and_sweeps_grants() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    await DeleteServer(uow=uow)(community_id=community, server_id=server.id)
    assert server.id not in uow.servers.by_id
    assert uow.resource_grants.swept == [("server", server.id.value)]
    assert uow.commits == 1


async def test_delete_rejects_while_running() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    with pytest.raises(ServerNotStoppedError):
        await DeleteServer(uow=uow)(community_id=community, server_id=server.id)
    assert server.id in uow.servers.by_id
    assert uow.resource_grants.swept == []
    assert uow.commits == 0


async def test_delete_other_communitys_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server(community_id=CommunityId(uuid.uuid4()))
    uow.servers.seed(server)
    with pytest.raises(ServerNotFoundError):
        await DeleteServer(uow=uow)(
            community_id=CommunityId(uuid.uuid4()), server_id=server.id
        )
    assert uow.resource_grants.swept == []


# --- create EULA seeding over real fs Storage ------------------------------


async def test_create_with_accept_eula_lands_at_rest_and_hydrates(
    tmp_path: Path,
) -> None:
    # End-to-end over a real FsStorage (no Storage-side fakes): accept_eula seeds
    # eula.txt into the initial published working set, so it is readable at rest
    # and present in the hydrate stream the Worker pulls on first start (#198).
    storage = FsStorage(tmp_path)
    file_store = StorageFileStoreAdapter(storage=storage)
    community = CommunityId(uuid.uuid4())
    server = await CreateServer(
        uow=FakeUnitOfWork(),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
    )(
        community_id=community,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
        accept_eula=True,
    )

    at_rest = await file_store.read_file(
        community_id=community, server_id=server.id, rel_path="eula.txt"
    )
    assert at_rest == b"eula=true\n"

    blob = await drain(
        storage.open_hydrate_source(
            StorageCommunityId(community.value),
            StorageServerId(server.id.value),
        )
    )
    # Both seeds compose into the first published working set (#243 + #198).
    assert read_tar(blob) == {
        "server.properties": b"server-port=25565\n",
        "eula.txt": b"eula=true\n",
    }


async def test_create_without_accept_eula_seeds_properties_at_rest(
    tmp_path: Path,
) -> None:
    # Without accept_eula, server.properties is still seeded (port assignment,
    # #243): it is readable at rest and in the hydrate stream, but eula.txt is not.
    storage = FsStorage(tmp_path)
    file_store = StorageFileStoreAdapter(storage=storage)
    community = CommunityId(uuid.uuid4())
    server = await CreateServer(
        uow=FakeUnitOfWork(),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
    )(
        community_id=community,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )

    at_rest = await file_store.read_file(
        community_id=community, server_id=server.id, rel_path="server.properties"
    )
    assert at_rest == b"server-port=25565\n"
    with pytest.raises(ServerFileNotFoundError):
        await file_store.read_file(
            community_id=community, server_id=server.id, rel_path="eula.txt"
        )
