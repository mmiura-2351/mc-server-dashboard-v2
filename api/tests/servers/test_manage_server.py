"""Use-case tests for server CRUD against in-memory fakes (TESTING.md Section 4).

Covers create (+ backend/type validation), community-scoped read/list,
cross-community not-found, update editability rules (backend immutable, at-rest
gate, name clash), and delete (at-rest gate + grant sweep atomicity).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
    ListServers,
    ReadServer,
    UpdateServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.cpu_allocation import (
    CPU_ALLOCATION_CONFIG_KEY,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ExecutionBackendImmutableError,
    InvalidBackupScheduleError,
    InvalidCpuAllocationError,
    InvalidMemoryLimitError,
    InvalidSnapshotIntervalError,
    PermissionDeniedError,
    PortAlreadyTakenError,
    PortOutOfRangeError,
    PortRangeExhaustedError,
    RemovedExecutionBackendError,
    ServerFileNotFoundError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    ServerNotStoppedError,
    UnknownExecutionBackendError,
    UnknownServerTypeError,
    UnsupportedEditionError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.memory_limit import (
    MEMORY_LIMIT_CONFIG_KEY,
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
    SpigotUnsupportedError,
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
    FakeBackupArchiveStore,
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
    game_port: int | None = None,
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
        game_port=game_port,
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
        execution_backend="container",
        config={},
    )
    assert server.mc_edition == "java"
    assert uow.commits == 1


async def test_create_accepts_valid_memory_limit() -> None:
    # A per-server memory limit on config round-trips through create (#705).
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
        execution_backend="container",
        config={MEMORY_LIMIT_CONFIG_KEY: 2048},
    )
    assert server.config[MEMORY_LIMIT_CONFIG_KEY] == 2048
    assert uow.commits == 1


async def test_create_rejects_invalid_memory_limit() -> None:
    # A below-floor memory limit 422s before the row is staged (#705).
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidMemoryLimitError):
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
            execution_backend="container",
            config={MEMORY_LIMIT_CONFIG_KEY: 1},
        )
    assert uow.commits == 0


async def test_create_without_memory_limit_writes_no_key() -> None:
    # Default-unset: no memory_limit_mb key is written, behavior unchanged (#705).
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
        execution_backend="container",
        config={"motd": "hi"},
    )
    assert MEMORY_LIMIT_CONFIG_KEY not in server.config


async def test_create_applies_default_memory_limit() -> None:
    # When DEFAULT_MEMORY_LIMIT_MB is set and the create config has no memory_limit_mb,
    # the default is injected into the config blob (issue #1069).
    uow = FakeUnitOfWork()
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=_PORTS,
        default_memory_limit_mb=2048,
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={"motd": "hi"},
    )
    assert server.config[MEMORY_LIMIT_CONFIG_KEY] == 2048


async def test_create_explicit_memory_limit_overrides_default() -> None:
    # An explicit memory_limit_mb in config takes precedence over the default (#1069).
    uow = FakeUnitOfWork()
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=_PORTS,
        default_memory_limit_mb=2048,
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={MEMORY_LIMIT_CONFIG_KEY: 4096},
    )
    assert server.config[MEMORY_LIMIT_CONFIG_KEY] == 4096


async def test_create_rejects_memory_limit_above_custom_max() -> None:
    # When MAX_MEMORY_LIMIT_MB is configured, a value above it is rejected (#1069).
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidMemoryLimitError):
        await CreateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(),
            file_store=FakeFileStore(),
            port_range=_PORTS,
            max_memory_limit_mb=4096,
        )(
            community_id=CommunityId(uuid.uuid4()),
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            execution_backend="container",
            config={MEMORY_LIMIT_CONFIG_KEY: 4097},
        )
    assert uow.commits == 0


async def test_create_accepts_memory_limit_at_custom_max() -> None:
    # A value equal to the custom max is accepted (#1069).
    uow = FakeUnitOfWork()
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=_PORTS,
        max_memory_limit_mb=4096,
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={MEMORY_LIMIT_CONFIG_KEY: 4096},
    )
    assert server.config[MEMORY_LIMIT_CONFIG_KEY] == 4096


async def test_create_accepts_valid_cpu_allocation() -> None:
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
        execution_backend="container",
        config={CPU_ALLOCATION_CONFIG_KEY: 2000},
    )
    assert server.config[CPU_ALLOCATION_CONFIG_KEY] == 2000
    assert uow.commits == 1


async def test_create_rejects_invalid_cpu_allocation() -> None:
    # A below-floor CPU allocation 422s before the row is staged (#722).
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidCpuAllocationError):
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
            execution_backend="container",
            config={CPU_ALLOCATION_CONFIG_KEY: 1},
        )
    assert uow.commits == 0


async def test_create_without_cpu_allocation_writes_no_key() -> None:
    # Default-unset: no cpu_millis key is written, behavior unchanged (#722).
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
        execution_backend="container",
        config={"motd": "hi"},
    )
    assert CPU_ALLOCATION_CONFIG_KEY not in server.config


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
            execution_backend="container",
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
            execution_backend="container",
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


async def test_create_rejects_removed_host_process_backend() -> None:
    # host_process is a known enum value retained for historical rows (#781), but
    # the driver was removed, so a freshly-created host_process server would be
    # unplaceable. Create rejects it (distinct from a wholly unknown backend) and
    # never stages a row.
    uow = FakeUnitOfWork()
    with pytest.raises(RemovedExecutionBackendError):
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
            execution_backend="host_process",
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
            execution_backend="container",
            config={},
        )
    assert uow.commits == 0


async def test_create_rejects_spigot_recommending_paper() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(SpigotUnsupportedError) as exc:
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
            server_type="spigot",
            execution_backend="container",
            config={},
        )
    assert "paper" in str(exc.value).lower()
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
            execution_backend="container",
            config={},
        )
    assert uow.commits == 0


# The server.properties content create seeds with port + the RCON keys (#243,
# #335). The password is the injected fixed token, so the expectation is exact.
_SEEDED_PROPERTIES = (
    b"server-port=25565\nenable-rcon=true\nrcon.port=25575\nrcon.password=tok\n"
)


async def test_create_with_accept_eula_seeds_eula_and_properties() -> None:
    # accept_eula=True composes eula.txt with the always-seeded server.properties
    # (port assignment #243 + RCON enablement #335): both land in the initial
    # working set, in order.
    uow = FakeUnitOfWork()
    file_store = FakeFileStore()
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
        token_generator=lambda: "tok",
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
        accept_eula=True,
    )
    assert file_store.writes == [
        ("server.properties", _SEEDED_PROPERTIES),
        ("eula.txt", b"eula=true\n"),
    ]
    assert file_store.files["eula.txt"] == b"eula=true\n"
    assert file_store.files["server.properties"] == _SEEDED_PROPERTIES
    assert uow.commits == 1
    assert uow.servers.by_id[server.id] is server


async def test_create_without_accept_eula_still_seeds_properties() -> None:
    # Default (accept_eula omitted): server.properties is still seeded with the
    # assigned game port (#243) and RCON keys (#335), but no eula.txt (issue #198
    # unchanged).
    uow = FakeUnitOfWork()
    file_store = FakeFileStore()
    await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
        token_generator=lambda: "tok",
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    assert file_store.writes == [("server.properties", _SEEDED_PROPERTIES)]
    assert "eula.txt" not in file_store.files


async def test_create_seeds_rcon_with_random_password_by_default() -> None:
    # Without an injected token generator, create still seeds all four properties
    # and the password is a non-empty random secret (default secrets-backed).
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
        execution_backend="container",
        config={},
    )
    seeded = file_store.files["server.properties"].decode()
    props = dict(line.split("=", 1) for line in seeded.splitlines() if "=" in line)
    assert props["server-port"] == "25565"
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == "25575"
    assert props["rcon.password"] != ""


# --- create: config overrides written to server.properties (#1209) ---------


async def test_create_merges_config_overrides_into_seeded_properties() -> None:
    # User-supplied server.properties overrides from config are merged into the
    # seeded server.properties at create time (issue #1209).
    uow = FakeUnitOfWork()
    file_store = FakeFileStore()
    await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
        token_generator=lambda: "tok",
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={"motd": "Hello World", "pvp": "false"},
    )
    expected = (
        b"server-port=25565\nenable-rcon=true\nrcon.port=25575\nrcon.password=tok\n"
        b"motd=Hello World\npvp=false\n"
    )
    assert file_store.files["server.properties"] == expected


async def test_create_skips_reserved_keys_in_properties_overrides() -> None:
    # Reserved keys (memory_limit_mb, cpu_millis, etc.) are NOT written to
    # server.properties — only user-supplied properties overrides are (#1209).
    uow = FakeUnitOfWork()
    file_store = FakeFileStore()
    await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
        token_generator=lambda: "tok",
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={MEMORY_LIMIT_CONFIG_KEY: 2048, "motd": "hi"},
    )
    props_text = file_store.files["server.properties"].decode()
    assert "memory_limit_mb" not in props_text
    assert "motd=hi" in props_text


async def test_create_skips_platform_managed_properties_keys() -> None:
    # Platform-managed server.properties keys (server-port, enable-rcon,
    # rcon.port, rcon.password) must NOT be overridable via the config dict
    # (#1243). They are set by _seed_initial_working_set and must not be
    # clobbered by user-supplied config values.
    uow = FakeUnitOfWork()
    file_store = FakeFileStore()
    await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
        token_generator=lambda: "tok",
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={
            "server-port": "99999",
            "enable-rcon": "false",
            "rcon.port": "11111",
            "rcon.password": "hacked",
            "motd": "hello",
        },
    )
    props_text = file_store.files["server.properties"].decode()
    # The platform-assigned values must survive, not the user overrides.
    assert "server-port=25565" in props_text
    assert "enable-rcon=true" in props_text
    assert "rcon.port=25575" in props_text
    assert "rcon.password=tok" in props_text
    # Legitimate user overrides still apply.
    assert "motd=hello" in props_text


async def test_create_skips_resource_pack_properties_keys() -> None:
    # Resource-pack keys (resource-pack, resource-pack-sha1,
    # require-resource-pack, resource-pack-prompt) are platform-managed via
    # set_resource_pack_properties and must NOT be overridable via the config
    # dict (#1253).
    uow = FakeUnitOfWork()
    file_store = FakeFileStore()
    await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
        token_generator=lambda: "tok",
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={
            "resource-pack": "http://evil.example.com/pack.zip",
            "resource-pack-sha1": "deadbeef",
            "require-resource-pack": "true",
            "resource-pack-prompt": "Install now!",
            "motd": "hello",
        },
    )
    props_text = file_store.files["server.properties"].decode()
    # None of the resource-pack keys should appear in server.properties.
    assert "resource-pack=" not in props_text
    assert "resource-pack-sha1=" not in props_text
    assert "require-resource-pack=" not in props_text
    assert "resource-pack-prompt=" not in props_text
    # Legitimate user overrides still apply.
    assert "motd=hello" in props_text


async def test_create_with_empty_config_seeds_no_overrides() -> None:
    # An empty config dict produces the standard seeded properties, no extras.
    uow = FakeUnitOfWork()
    file_store = FakeFileStore()
    await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
        token_generator=lambda: "tok",
    )(
        community_id=CommunityId(uuid.uuid4()),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    assert file_store.files["server.properties"] == _SEEDED_PROPERTIES


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
        token_generator=lambda: "tok",
    )(
        community_id=community,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    assert server.game_port == 25567
    assert uow.servers.by_id[server.id].game_port == 25567
    assert file_store.writes == [
        (
            "server.properties",
            b"server-port=25567\nenable-rcon=true\nrcon.port=25575\nrcon.password=tok\n",
        )
    ]


async def test_create_skips_relay_reserved_port() -> None:
    # With relay enabled, the relay's game port (25565) is reserved on the
    # PortRange; auto-assign must skip it even when it is the lowest free port,
    # so the server never collides with the relay's host bind (issue #1002).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    relay_range = PortRange(start=25565, end=25664, reserved=frozenset({25565, 25665}))
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=relay_range,
    )(
        community_id=community,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    assert server.game_port == 25566
    assert uow.servers.by_id[server.id].game_port == 25566


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
        execution_backend="container",
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
            execution_backend="container",
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
            execution_backend="container",
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
            execution_backend="container",
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
            execution_backend="container",
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


async def _grant_all(_code: str) -> bool:
    """A permissive ``authorize`` for tests that exercise non-authz behavior."""

    return True


def _updater(
    uow: FakeUnitOfWork,
    *,
    file_store: FakeFileStore | None = None,
    min_interval_seconds: int = 0,
) -> Callable[..., Awaitable[Server]]:
    """Build an :class:`UpdateServer` with the file seam + port range bound (#311).

    ``authorize`` is required on :class:`UpdateServer` (issue #458). Call sites that
    do not exercise the gate omit it and get a permit-all default; gate tests pass an
    explicit ``authorize=`` which overrides the default.
    """

    use_case = UpdateServer(
        uow=uow,
        clock=FakeClock(_LATER),
        file_store=file_store or FakeFileStore(),
        port_range=_PORTS,
        min_interval_seconds=min_interval_seconds,
    )

    async def call(
        *, authorize: Callable[..., Awaitable[bool]] = _grant_all, **kwargs: Any
    ) -> Server:
        return await use_case(authorize=authorize, **kwargs)

    return call


async def test_update_edits_name_and_config_while_at_rest() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    updated = await _updater(uow)(
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
    updated = await _updater(uow, min_interval_seconds=300)(
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
        await _updater(uow, min_interval_seconds=300)(
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
    updated = await _updater(uow)(
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
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            config={"backup_interval_hours": 0},
        )
    assert uow.commits == 0


async def test_update_accepts_valid_memory_limit() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    updated = await _updater(uow)(
        community_id=community,
        server_id=server.id,
        config={MEMORY_LIMIT_CONFIG_KEY: 4096},
    )
    assert updated.config[MEMORY_LIMIT_CONFIG_KEY] == 4096
    assert uow.commits == 1


async def test_update_rejects_invalid_memory_limit() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(InvalidMemoryLimitError):
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            config={MEMORY_LIMIT_CONFIG_KEY: 1},
        )
    assert uow.commits == 0


async def test_update_rejects_memory_limit_above_custom_max() -> None:
    # When max_memory_limit_mb is configured, values above it are rejected (#1069).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    use_case = UpdateServer(
        uow=uow,
        clock=FakeClock(_LATER),
        file_store=FakeFileStore(),
        port_range=_PORTS,
        max_memory_limit_mb=4096,
    )
    with pytest.raises(InvalidMemoryLimitError):
        await use_case(
            community_id=community,
            server_id=server.id,
            config={MEMORY_LIMIT_CONFIG_KEY: 4097},
            authorize=_grant_all,
        )
    assert uow.commits == 0


async def test_update_accepts_valid_cpu_allocation() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    updated = await _updater(uow)(
        community_id=community,
        server_id=server.id,
        config={CPU_ALLOCATION_CONFIG_KEY: 4000},
    )
    assert updated.config[CPU_ALLOCATION_CONFIG_KEY] == 4000
    assert uow.commits == 1


async def test_update_rejects_invalid_cpu_allocation() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(InvalidCpuAllocationError):
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            config={CPU_ALLOCATION_CONFIG_KEY: 1},
        )
    assert uow.commits == 0


# --- update permission gate (issue #458) -----------------------------------
#
# A config edit that touches only the backup-scheduling key
# (``backup_interval_hours``) requires ``backup:schedule``; any other change
# requires ``server:update``; a mixed edit requires both. ``server:update`` no
# longer implies scheduling. The gate keys off the *changed* set vs the current
# config, so it sees what the edit actually alters (not the round-tripped blob).


class _Grant:
    """An ``authorize`` callback granting exactly ``codes`` (denying anything else).

    Records the codes it was asked about in ``seen`` so tests can assert the gate
    queried only the permissions the changed-key set requires.
    """

    def __init__(self, *codes: str) -> None:
        self._granted = set(codes)
        self.seen: list[str] = []

    async def __call__(self, code: str) -> bool:
        self.seen.append(code)
        return code in self._granted


def _grant(*codes: str) -> _Grant:
    return _Grant(*codes)


async def test_update_scheduling_only_requires_backup_schedule() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    authorize = _grant("backup:schedule")
    updated = await _updater(uow)(
        community_id=community,
        server_id=server.id,
        config={"motd": "hi", "backup_interval_hours": 6},
        authorize=authorize,
    )
    assert updated.config["backup_interval_hours"] == 6
    assert authorize.seen == ["backup:schedule"]
    assert uow.commits == 1


async def test_update_scheduling_only_denied_without_backup_schedule() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(PermissionDeniedError) as exc:
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            config={"motd": "hi", "backup_interval_hours": 6},
            authorize=_grant("server:update"),
        )
    assert exc.value.permission == "backup:schedule"
    assert uow.commits == 0


async def test_update_non_scheduling_change_requires_server_update() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    authorize = _grant("server:update")
    updated = await _updater(uow)(
        community_id=community,
        server_id=server.id,
        name="creative",
        authorize=authorize,
    )
    assert updated.name == ServerName("creative")
    assert authorize.seen == ["server:update"]
    assert uow.commits == 1


async def test_update_non_scheduling_change_denied_without_server_update() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(PermissionDeniedError) as exc:
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            name="creative",
            authorize=_grant("backup:schedule"),
        )
    assert exc.value.permission == "server:update"
    assert uow.commits == 0


async def test_update_mixed_change_requires_both_permissions() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    updated = await _updater(uow)(
        community_id=community,
        server_id=server.id,
        name="creative",
        config={"motd": "hi", "backup_interval_hours": 6},
        authorize=_grant("server:update", "backup:schedule"),
    )
    assert updated.name == ServerName("creative")
    assert updated.config["backup_interval_hours"] == 6
    assert uow.commits == 1


async def test_update_mixed_change_denied_missing_server_update() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(PermissionDeniedError) as exc:
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            name="creative",
            config={"motd": "hi", "backup_interval_hours": 6},
            authorize=_grant("backup:schedule"),
        )
    assert exc.value.permission == "server:update"
    assert uow.commits == 0


async def test_update_mixed_change_denied_missing_backup_schedule() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(PermissionDeniedError) as exc:
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            name="creative",
            config={"motd": "hi", "backup_interval_hours": 6},
            authorize=_grant("server:update"),
        )
    assert exc.value.permission == "backup:schedule"
    assert uow.commits == 0


async def test_update_snapshot_interval_change_requires_server_update() -> None:
    # snapshot_interval_seconds has no dedicated permission code; it stays under
    # server:update (only the backup-scheduling key maps to backup:schedule).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(PermissionDeniedError) as exc:
        await _updater(uow, min_interval_seconds=300)(
            community_id=community,
            server_id=server.id,
            config={"motd": "hi", "snapshot_interval_seconds": 600},
            authorize=_grant("backup:schedule"),
        )
    assert exc.value.permission == "server:update"
    assert uow.commits == 0


async def test_update_memory_limit_change_requires_server_update() -> None:
    # memory_limit_mb has no dedicated permission code; like any non-scheduling
    # config key it is gated by server:update (#705 reuses the existing gate).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(PermissionDeniedError) as exc:
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            config={"motd": "hi", MEMORY_LIMIT_CONFIG_KEY: 2048},
            authorize=_grant("backup:schedule"),
        )
    assert exc.value.permission == "server:update"
    assert uow.commits == 0


async def test_update_cpu_allocation_change_requires_server_update() -> None:
    # cpu_millis has no dedicated permission code; like any non-scheduling config
    # key it is gated by server:update (#722 reuses the existing gate).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(PermissionDeniedError) as exc:
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            config={"motd": "hi", CPU_ALLOCATION_CONFIG_KEY: 2000},
            authorize=_grant("backup:schedule"),
        )
    assert exc.value.permission == "server:update"
    assert uow.commits == 0


async def test_update_empty_body_requires_server_update() -> None:
    # A PATCH that asserts an edit but touches nothing (empty body) must still
    # require server:update — the conservative pre-#458 default. Otherwise a caller
    # with no permissions could use the 200 as an info leak / state oracle.
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(PermissionDeniedError) as exc:
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            authorize=_grant("backup:schedule"),
        )
    assert exc.value.permission == "server:update"
    assert uow.commits == 0


async def test_update_no_op_config_requires_server_update() -> None:
    # A config that round-trips identical to the current config has an empty
    # changed-key set, but the PATCH still requires server:update.
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    with pytest.raises(PermissionDeniedError) as exc:
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            config=dict(server.config),
            authorize=_grant("backup:schedule"),
        )
    assert exc.value.permission == "server:update"
    assert uow.commits == 0


async def test_update_empty_body_succeeds_with_server_update() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    authorize = _grant("server:update")
    await _updater(uow)(
        community_id=community,
        server_id=server.id,
        authorize=authorize,
    )
    assert authorize.seen == ["server:update"]
    assert uow.commits == 1
    assert uow.commits == 1


async def test_update_rejects_backend_change_as_immutable() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community, backend=ExecutionBackend.HOST_PROCESS)
    uow.servers.seed(server)
    with pytest.raises(ExecutionBackendImmutableError):
        await _updater(uow)(
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
    updated = await _updater(uow)(
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
        await _updater(uow)(
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
    updated = await _updater(uow, min_interval_seconds=300)(
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
        await _updater(uow)(
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
        await _updater(uow, min_interval_seconds=300)(
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
        await _updater(uow, min_interval_seconds=300)(
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
        await _updater(uow, min_interval_seconds=300)(
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
        await _updater(uow, min_interval_seconds=300)(
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
        await _updater(uow, min_interval_seconds=300)(
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
        await _updater(uow)(
            community_id=community,
            server_id=target.id,
            name="taken",
        )


async def test_update_other_communitys_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server(community_id=CommunityId(uuid.uuid4()))
    uow.servers.seed(server)
    with pytest.raises(ServerNotFoundError):
        await _updater(uow)(
            community_id=CommunityId(uuid.uuid4()),
            server_id=server.id,
            name="x",
        )


# --- update: config overrides synced to server.properties (#1209) ----------


async def test_update_config_overrides_syncs_to_properties_file() -> None:
    # When config overrides change, the new overrides are written into
    # server.properties (issue #1209).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    server.config = {"motd": "old"}
    uow.servers.seed(server)
    file_store = FakeFileStore()
    file_store.files["server.properties"] = b"server-port=25565\nmotd=old\n"
    updated = await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        config={"motd": "new"},
    )
    assert updated.config == {"motd": "new"}
    assert file_store.files["server.properties"] == b"server-port=25565\nmotd=new\n"
    assert uow.commits == 1


async def test_update_config_overrides_skips_reserved_keys_in_file() -> None:
    # Reserved keys (memory_limit_mb, etc.) are NOT written to server.properties.
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    server.config = {"motd": "hi"}
    uow.servers.seed(server)
    file_store = FakeFileStore()
    file_store.files["server.properties"] = b"server-port=25565\nmotd=hi\n"
    updated = await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        config={"motd": "hi", MEMORY_LIMIT_CONFIG_KEY: 2048},
    )
    assert updated.config[MEMORY_LIMIT_CONFIG_KEY] == 2048
    props_text = file_store.files["server.properties"].decode()
    assert "memory_limit_mb" not in props_text
    assert uow.commits == 1


async def test_update_config_overrides_skips_platform_managed_keys() -> None:
    # Platform-managed server.properties keys (server-port, enable-rcon,
    # rcon.port, rcon.password) must NOT be written into server.properties via
    # config overrides (#1243). They are managed by platform seeding and the
    # port-rewrite path; a user config must not clobber them.
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    server.config = {"motd": "hi"}
    uow.servers.seed(server)
    file_store = FakeFileStore()
    file_store.files["server.properties"] = (
        b"server-port=25565\nenable-rcon=true\nrcon.port=25575\n"
        b"rcon.password=secret\nmotd=hi\n"
    )
    await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        config={
            "motd": "bye",
            "server-port": "99999",
            "enable-rcon": "false",
            "rcon.port": "11111",
            "rcon.password": "hacked",
        },
    )
    # The config dict stores all keys (the DB row is the source of truth for
    # non-properties keys), but the file must not contain the overridden values.
    props_text = file_store.files["server.properties"].decode()
    assert "server-port=25565" in props_text
    assert "enable-rcon=true" in props_text
    assert "rcon.port=25575" in props_text
    assert "rcon.password=secret" in props_text
    assert "motd=bye" in props_text
    assert uow.commits == 1


async def test_update_config_overrides_skips_resource_pack_keys() -> None:
    # Resource-pack keys are platform-managed via set_resource_pack_properties
    # and must NOT be written into server.properties via config overrides
    # (#1253).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    server.config = {"motd": "hi"}
    uow.servers.seed(server)
    file_store = FakeFileStore()
    file_store.files["server.properties"] = b"server-port=25565\nmotd=hi\n"
    await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        config={
            "motd": "bye",
            "resource-pack": "http://evil.example.com/pack.zip",
            "resource-pack-sha1": "deadbeef",
            "require-resource-pack": "true",
            "resource-pack-prompt": "Install now!",
        },
    )
    props_text = file_store.files["server.properties"].decode()
    # None of the resource-pack keys should appear in server.properties.
    assert "resource-pack=" not in props_text
    assert "resource-pack-sha1=" not in props_text
    assert "require-resource-pack=" not in props_text
    assert "resource-pack-prompt=" not in props_text
    # Legitimate user overrides still apply.
    assert "motd=bye" in props_text
    assert uow.commits == 1


async def test_update_no_override_change_skips_file_rewrite() -> None:
    # When the properties overrides are unchanged, the file is not rewritten.
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    server.config = {"motd": "hi"}
    uow.servers.seed(server)
    file_store = FakeFileStore()
    updated = await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        config={"motd": "hi"},
    )
    assert updated.config == {"motd": "hi"}
    # No file writes at all (the overrides did not change).
    assert file_store.writes == []
    assert uow.commits == 1


async def test_update_config_overrides_creates_properties_when_absent() -> None:
    # A legacy server with no server.properties gets one created from the overrides.
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    server.config = {}
    uow.servers.seed(server)
    file_store = FakeFileStore()
    updated = await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        config={"motd": "hello"},
    )
    assert updated.config == {"motd": "hello"}
    assert file_store.files["server.properties"] == b"motd=hello\n"


async def test_update_config_overrides_file_failure_aborts_without_commit() -> None:
    # A storage failure rewriting server.properties surfaces as a seed failure and
    # leaves the config row uncommitted, so the DB and file do not drift (#1209).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    server.config = {"motd": "old"}
    uow.servers.seed(server)
    with pytest.raises(WorkingSetSeedFailedError):
        await _updater(uow, file_store=FakeFileStore(fail_write=True))(
            community_id=community,
            server_id=server.id,
            config={"motd": "new"},
        )
    assert uow.commits == 0


async def test_update_config_removing_override_clears_key_from_file() -> None:
    # When a user removes a key from config overrides, the corresponding line
    # must be removed from server.properties (issue #1242).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    server.config = {"motd": "hi", "pvp": "true"}
    uow.servers.seed(server)
    file_store = FakeFileStore()
    file_store.files["server.properties"] = b"server-port=25565\nmotd=hi\npvp=true\n"
    updated = await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        config={"motd": "hi"},
    )
    assert updated.config == {"motd": "hi"}
    props_text = file_store.files["server.properties"].decode()
    assert "motd=hi" in props_text
    assert "pvp" not in props_text
    assert "server-port=25565" in props_text


# --- update: game port (#311) ----------------------------------------------


async def test_update_game_port_updates_row_and_rewrites_properties() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community, game_port=25565)
    uow.servers.seed(server)
    file_store = FakeFileStore()
    file_store.files["server.properties"] = b"motd=hi\nserver-port=25565\n"
    updated = await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        game_port=25570,
    )
    # The DB row and the at-rest server.properties are both moved to the new port.
    assert updated.game_port == 25570
    assert uow.servers.by_id[server.id].game_port == 25570
    assert file_store.files["server.properties"] == b"motd=hi\nserver-port=25570\n"
    assert uow.commits == 1


async def test_update_game_port_creates_properties_when_absent() -> None:
    # A legacy server with no seeded server.properties (#243) gets one created
    # with just the port line.
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community, game_port=None)
    uow.servers.seed(server)
    file_store = FakeFileStore()
    updated = await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        game_port=25570,
    )
    assert updated.game_port == 25570
    assert file_store.files["server.properties"] == b"server-port=25570\n"


async def test_update_game_port_rejects_while_running() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        game_port=25565,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    with pytest.raises(ServerNotStoppedError):
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            game_port=25570,
        )
    assert uow.commits == 0


async def test_update_game_port_rejects_taken_port() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    target = _server(community_id=community, name="a", game_port=25565)
    other = _server(community_id=community, name="b", game_port=25570)
    uow.servers.seed(target)
    uow.servers.seed(other)
    with pytest.raises(PortAlreadyTakenError):
        await _updater(uow)(
            community_id=community,
            server_id=target.id,
            game_port=25570,
        )
    assert uow.commits == 0


async def test_update_game_port_rejects_out_of_range() -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community, game_port=25565)
    uow.servers.seed(server)
    with pytest.raises(PortOutOfRangeError):
        await _updater(uow)(
            community_id=community,
            server_id=server.id,
            game_port=70000,
        )
    assert uow.commits == 0


async def test_update_to_same_game_port_does_not_rewrite_or_conflict() -> None:
    # Re-supplying the current port is a no-op for the file (no rewrite) and never
    # a self-conflict (the own port is excluded from the taken set).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community, game_port=25565)
    uow.servers.seed(server)
    file_store = FakeFileStore()
    updated = await _updater(uow, file_store=file_store)(
        community_id=community,
        server_id=server.id,
        game_port=25565,
    )
    assert updated.game_port == 25565
    assert file_store.writes == []
    assert uow.commits == 1


async def test_update_game_port_file_failure_aborts_without_commit() -> None:
    # A storage failure rewriting server.properties surfaces as a seed failure and
    # leaves the row uncommitted, so the DB and file do not drift (#311).
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community, game_port=25565)
    uow.servers.seed(server)
    with pytest.raises(WorkingSetSeedFailedError):
        await _updater(uow, file_store=FakeFileStore(fail_write=True))(
            community_id=community,
            server_id=server.id,
            game_port=25570,
        )
    assert uow.commits == 0
    assert uow.servers.by_id[server.id].game_port == 25565


# --- delete ----------------------------------------------------------------


def _backup(server_id: ServerId, *, ref: str, created_at: dt.datetime) -> Backup:
    return Backup(
        id=BackupId(uuid.uuid4()),
        server_id=server_id,
        storage_ref=ref,
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=created_at,
    )


async def test_delete_removes_server_and_sweeps_grants() -> None:
    uow = FakeUnitOfWork()
    store = FakeBackupArchiveStore()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    await DeleteServer(uow=uow, backup_store=store)(
        community_id=community, server_id=server.id
    )
    assert server.id not in uow.servers.by_id
    assert uow.resource_grants.swept == [("server", server.id.value)]
    assert uow.commits == 1
    # The working set is always packed into the retained final tar.gz (#777).
    assert store.pruned == [server.id]


async def test_delete_with_backups_keeps_newest_archive_and_prunes_the_rest() -> None:
    # Retention (#777): only the newest backup archive survives, plus the packed
    # working set; every older archive is deleted (archive-first).
    uow = FakeUnitOfWork()
    store = FakeBackupArchiveStore()
    store.archives.update({"old", "mid", "new"})
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    base = dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc)
    uow.backups.seed(_backup(server.id, ref="old", created_at=base))
    uow.backups.seed(
        _backup(server.id, ref="mid", created_at=base + dt.timedelta(hours=1))
    )
    uow.backups.seed(
        _backup(server.id, ref="new", created_at=base + dt.timedelta(hours=2))
    )
    await DeleteServer(uow=uow, backup_store=store)(
        community_id=community, server_id=server.id
    )
    # The newest ("new") is retained; the two older archives are deleted.
    assert {ref for _, ref in store.deleted} == {"old", "mid"}
    assert store.archives == {"new"}
    assert store.pruned == [server.id]
    assert server.id not in uow.servers.by_id


async def test_delete_with_no_backups_keeps_only_the_packed_working_set() -> None:
    uow = FakeUnitOfWork()
    store = FakeBackupArchiveStore()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    await DeleteServer(uow=uow, backup_store=store)(
        community_id=community, server_id=server.id
    )
    # No archive to keep or delete; only the working-set pack runs.
    assert store.deleted == []
    assert store.pruned == [server.id]
    assert server.id not in uow.servers.by_id


async def test_delete_aborts_when_packing_the_working_set_fails() -> None:
    # The working-set pack is mandatory and fail-closed (#777): if it fails the row
    # survives, no archive is deleted, and the delete is retryable.
    uow = FakeUnitOfWork()
    store = FakeBackupArchiveStore(pack_fails=True)
    store.archives.update({"old", "new"})
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    base = dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc)
    uow.backups.seed(_backup(server.id, ref="old", created_at=base))
    uow.backups.seed(
        _backup(server.id, ref="new", created_at=base + dt.timedelta(hours=1))
    )
    with pytest.raises(RuntimeError):
        await DeleteServer(uow=uow, backup_store=store)(
            community_id=community, server_id=server.id
        )
    assert server.id in uow.servers.by_id
    assert uow.resource_grants.swept == []
    assert uow.commits == 0
    # Nothing was deleted: the pack fails before any archive prune (intact state).
    assert store.deleted == []
    assert store.archives == {"old", "new"}


async def test_delete_rejects_while_running() -> None:
    uow = FakeUnitOfWork()
    store = FakeBackupArchiveStore()
    community = CommunityId(uuid.uuid4())
    server = _server(
        community_id=community,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    with pytest.raises(ServerNotStoppedError):
        await DeleteServer(uow=uow, backup_store=store)(
            community_id=community, server_id=server.id
        )
    assert server.id in uow.servers.by_id
    assert uow.resource_grants.swept == []
    assert uow.commits == 0
    # A running server's working set is never touched.
    assert store.pruned == []


async def test_delete_rechecks_at_rest_after_the_pack_window() -> None:
    # Two-transaction TOCTOU bound (#777 review): a start that lands DURING the
    # (possibly minutes-long) pack must not delete a now-running server's row. The
    # final transaction re-checks is_at_rest() before the row delete, so a server
    # that started in the pack window is rejected and its row survives.
    uow = FakeUnitOfWork()
    store = FakeBackupArchiveStore()
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)

    def _start_during_pack() -> None:
        server.desired_state = DesiredState.RUNNING
        server.observed_state = ObservedState.RUNNING

    store.on_prune = _start_during_pack

    with pytest.raises(ServerNotStoppedError):
        await DeleteServer(uow=uow, backup_store=store)(
            community_id=community, server_id=server.id
        )
    # The pack already ran (working set packed) but the row survives and no grants
    # were swept: the delete is rejected and retryable once the server stops again.
    assert store.pruned == [server.id]
    assert server.id in uow.servers.by_id
    assert uow.resource_grants.swept == []
    assert uow.commits == 0


async def test_delete_reconciles_a_backup_created_during_the_pack_window() -> None:
    # Third-orphan fix (#777 review): a backup created during the (minutes-long) pack
    # would, if the list were snapshotted before the pack, be neither the retained head
    # nor in the deletable tail — its archive would survive as a third orphan. The list
    # is re-read in the final transaction, so the mid-pack backup becomes the head
    # ("latest existing at delete time") and the previously-newest archive is deleted.
    uow = FakeUnitOfWork()
    store = FakeBackupArchiveStore()
    store.archives.update({"old", "new", "midpack"})
    community = CommunityId(uuid.uuid4())
    server = _server(community_id=community)
    uow.servers.seed(server)
    base = dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc)
    uow.backups.seed(_backup(server.id, ref="old", created_at=base))
    uow.backups.seed(
        _backup(server.id, ref="new", created_at=base + dt.timedelta(hours=1))
    )

    def _backup_created_during_pack() -> None:
        uow.backups.seed(
            _backup(server.id, ref="midpack", created_at=base + dt.timedelta(hours=2))
        )

    store.on_prune = _backup_created_during_pack

    await DeleteServer(uow=uow, backup_store=store)(
        community_id=community, server_id=server.id
    )
    # The mid-pack backup is the newest by created_at at delete time, so it is the
    # retained head; "old" and "new" are both deleted — no third orphan survives.
    assert {ref for _, ref in store.deleted} == {"old", "new"}
    assert store.archives == {"midpack"}
    assert store.pruned == [server.id]
    assert server.id not in uow.servers.by_id


async def test_delete_other_communitys_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    store = FakeBackupArchiveStore()
    server = _server(community_id=CommunityId(uuid.uuid4()))
    uow.servers.seed(server)
    with pytest.raises(ServerNotFoundError):
        await DeleteServer(uow=uow, backup_store=store)(
            community_id=CommunityId(uuid.uuid4()), server_id=server.id
        )
    assert uow.resource_grants.swept == []
    assert store.pruned == []


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
        token_generator=lambda: "tok",
    )(
        community_id=community,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
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
    # Both seeds compose into the first published working set (#243 + #198 + #335).
    assert read_tar(blob) == {
        "server.properties": _SEEDED_PROPERTIES,
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
        token_generator=lambda: "tok",
    )(
        community_id=community,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )

    at_rest = await file_store.read_file(
        community_id=community, server_id=server.id, rel_path="server.properties"
    )
    assert at_rest == _SEEDED_PROPERTIES
    with pytest.raises(ServerFileNotFoundError):
        await file_store.read_file(
            community_id=community, server_id=server.id, rel_path="eula.txt"
        )


# --- update game port over real fs Storage (#311) --------------------------


async def test_update_game_port_rewrites_properties_at_rest(tmp_path: Path) -> None:
    # End-to-end over real FsStorage: create seeds server.properties on the
    # assigned port, then a port update rewrites server-port in place so the
    # at-rest file (and the hydrate stream) carry the new port.
    storage = FsStorage(tmp_path)
    file_store = StorageFileStoreAdapter(storage=storage)
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    server = await CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=_PORTS,
        token_generator=lambda: "tok",
    )(
        community_id=community,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    assert server.game_port == 25565

    updated = await UpdateServer(
        uow=uow,
        clock=FakeClock(_LATER),
        file_store=file_store,
        port_range=_PORTS,
    )(
        community_id=community,
        server_id=server.id,
        game_port=25570,
        authorize=_grant_all,
    )
    assert updated.game_port == 25570

    # The port rewrite changes server-port in place and leaves the seeded RCON keys
    # (#335) untouched.
    expected = (
        b"server-port=25570\nenable-rcon=true\nrcon.port=25575\nrcon.password=tok\n"
    )
    at_rest = await file_store.read_file(
        community_id=community, server_id=server.id, rel_path="server.properties"
    )
    assert at_rest == expected
    blob = await drain(
        storage.open_hydrate_source(
            StorageCommunityId(community.value),
            StorageServerId(server.id.value),
        )
    )
    assert read_tar(blob) == {"server.properties": expected}
