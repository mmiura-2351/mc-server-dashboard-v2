"""Use-case tests for server↔mod assignment & deployment (issue #1262).

Tests run against fakes (no database), following TESTING.md Section 4. The focus
is the physical deployment behaviour that distinguishes mods from resource packs:
target-dir selection by loader (``mods/`` vs ``plugins/``), side filtering
(client-only is NOT placed server-side), the enabled/disabled ``.disabled``
rename, the at-rest gate, and the lifecycle lock.
"""

from __future__ import annotations

import datetime as dt
import io
import random
import uuid
import zipfile
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.application.server_mods import (
    AssignMods,
    DownloadClientModpack,
    ListClientMods,
    ListServerMods,
    SetModEnabled,
    UnassignMod,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ModAssignmentNotFoundError,
    ModNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId, ModLoader, ModSide
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeLifecycleLock,
    FakeModStore,
    FakeServerRepository,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 19, 12, 0, 0, tzinfo=dt.timezone.utc)
_COMMUNITY_ID = CommunityId(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))


def _at_rest_server(server_id: ServerId | None = None) -> Server:
    return Server(
        id=server_id or ServerId(uuid.uuid4()),
        community_id=_COMMUNITY_ID,
        name=ServerName("test-server"),
        mc_edition="java",
        mc_version="1.21",
        server_type=ServerType("fabric"),
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _running_server(server_id: ServerId | None = None) -> Server:
    return Server(
        id=server_id or ServerId(uuid.uuid4()),
        community_id=_COMMUNITY_ID,
        name=ServerName("running-server"),
        mc_edition="java",
        mc_version="1.21",
        server_type=ServerType("fabric"),
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
        observed_at=_NOW,
        assigned_worker_id=WorkerId(uuid.uuid4()),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_mod(
    *,
    loader_type: ModLoader = "fabric",
    side: ModSide = "both",
    filename: str = "sodium.jar",
    sha256: str = "a" * 64,
) -> Mod:
    return Mod(
        id=ModId.new(),
        filename=filename,
        display_name=filename,
        description=None,
        loader_type=loader_type,
        mod_identifier="sodium",
        provides=[],
        version_number="0.5.0",
        mc_versions=["1.21"],
        side=side,
        dependencies=[],
        sha256_hash=sha256,
        sha512_hash=None,
        size_bytes=4,
        source="local",
        source_project_id=None,
        source_version_id=None,
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _seed_mod(uow: FakeUnitOfWork, store: FakeModStore, mod: Mod) -> Mod:
    uow.mods.mods[mod.id] = mod
    store.blobs[mod.id] = b"JAR!"
    return mod


def _ctx(server: Server) -> tuple[FakeUnitOfWork, FakeFileStore, FakeModStore]:
    servers = FakeServerRepository()
    servers.seed(server)
    return FakeUnitOfWork(servers=servers), FakeFileStore(), FakeModStore()


class TestAssignMods:
    async def test_assign_deploys_fabric_jar_into_mods_dir(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod(loader_type="fabric"))
        lock = FakeLifecycleLock()
        user_id = uuid.uuid4()

        uc = AssignMods(
            uow=uow,
            file_store=file_store,
            store=store,
            clock=FakeClock(_NOW),
            lifecycle_lock=lock,
        )
        result = await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=user_id,
        )

        assert len(result) == 1
        assert result[0].mod_id == mod.id
        assert result[0].enabled is True
        assert result[0].assigned_by == user_id
        # Jar physically placed into mods/.
        assert file_store.files["mods/sodium.jar"] == b"JAR!"
        # Row committed and lock held.
        assert uow.commits == 1
        assert lock.events == [(server.id, "acquire"), (server.id, "release")]

    async def test_assign_deploys_paper_jar_into_plugins_dir(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(
            uow, store, _make_mod(loader_type="paper", filename="essentials.jar")
        )

        uc = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )

        assert file_store.files["plugins/essentials.jar"] == b"JAR!"
        assert "mods/essentials.jar" not in file_store.files

    @pytest.mark.parametrize("loader", ["forge", "neoforge", "quilt"])
    async def test_assign_deploys_other_loaders_into_mods_dir(
        self, loader: ModLoader
    ) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod(loader_type=loader))

        uc = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )

        assert file_store.files["mods/sodium.jar"] == b"JAR!"

    async def test_assign_multiple_mods(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        m1 = _seed_mod(uow, store, _make_mod(filename="a.jar", sha256="1" * 64))
        m2 = _seed_mod(uow, store, _make_mod(filename="b.jar", sha256="2" * 64))

        uc = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        result = await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[m1.id, m2.id],
            assigned_by=uuid.uuid4(),
        )

        assert {a.mod_id for a in result} == {m1.id, m2.id}
        assert "mods/a.jar" in file_store.files
        assert "mods/b.jar" in file_store.files
        assert len(uow.mods.assignments) == 2

    async def test_assign_client_only_mod_not_deployed_server_side(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod(side="client"))

        uc = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        result = await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )

        # Row recorded, but no jar placed server-side.
        assert len(result) == 1
        assert file_store.files == {}

    async def test_assign_server_side_mod_is_deployed(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod(side="server"))

        uc = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )

        assert file_store.files["mods/sodium.jar"] == b"JAR!"

    async def test_assign_idempotent_for_already_assigned(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        uc = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )
        result = await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )

        assert len(result) == 1
        assert len(uow.mods.assignments) == 1

    async def test_assign_rejects_unknown_mod(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)

        uc = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        with pytest.raises(ModNotFoundError):
            await uc(
                community_id=_COMMUNITY_ID,
                server_id=server.id,
                mod_ids=[ModId.new()],
                assigned_by=uuid.uuid4(),
            )

    async def test_assign_rejects_unknown_server(self) -> None:
        uow = FakeUnitOfWork()
        uc = AssignMods(
            uow=uow,
            file_store=FakeFileStore(),
            store=FakeModStore(),
            clock=FakeClock(_NOW),
        )
        with pytest.raises(ServerNotFoundError):
            await uc(
                community_id=_COMMUNITY_ID,
                server_id=ServerId(uuid.uuid4()),
                mod_ids=[],
                assigned_by=uuid.uuid4(),
            )

    async def test_assign_rejects_running_server(self) -> None:
        server = _running_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        uc = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        with pytest.raises(ServerFilesUnsettledError):
            await uc(
                community_id=_COMMUNITY_ID,
                server_id=server.id,
                mod_ids=[mod.id],
                assigned_by=uuid.uuid4(),
            )


class TestUnassignMod:
    async def test_unassign_removes_row_and_jar(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())
        lock = FakeLifecycleLock()

        assign = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await assign(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )
        assert "mods/sodium.jar" in file_store.files

        uc = UnassignMod(
            uow=uow, file_store=file_store, store=store, lifecycle_lock=lock
        )
        await uc(community_id=_COMMUNITY_ID, server_id=server.id, mod_id=mod.id)

        assert len(uow.mods.assignments) == 0
        assert "mods/sodium.jar" not in file_store.files
        assert lock.events == [(server.id, "acquire"), (server.id, "release")]

    async def test_unassign_removes_disabled_variant(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        assign = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await assign(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )
        disable = SetModEnabled(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await disable(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_id=mod.id,
            enabled=False,
        )
        assert "mods/sodium.jar.disabled" in file_store.files

        uc = UnassignMod(uow=uow, file_store=file_store, store=store)
        await uc(community_id=_COMMUNITY_ID, server_id=server.id, mod_id=mod.id)

        assert "mods/sodium.jar.disabled" not in file_store.files

    async def test_unassign_rejects_unassigned(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        uc = UnassignMod(uow=uow, file_store=file_store, store=store)
        with pytest.raises(ModAssignmentNotFoundError):
            await uc(community_id=_COMMUNITY_ID, server_id=server.id, mod_id=mod.id)

    async def test_unassign_rejects_running_server(self) -> None:
        server = _running_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        uc = UnassignMod(uow=uow, file_store=file_store, store=store)
        with pytest.raises(ServerFilesUnsettledError):
            await uc(community_id=_COMMUNITY_ID, server_id=server.id, mod_id=mod.id)


class TestSetModEnabled:
    async def test_disable_renames_to_disabled(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        assign = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await assign(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )

        uc = SetModEnabled(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        assignment = await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_id=mod.id,
            enabled=False,
        )

        assert assignment.enabled is False
        assert "mods/sodium.jar" not in file_store.files
        assert file_store.files["mods/sodium.jar.disabled"] == b"JAR!"

    async def test_reenable_redeploys(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        assign = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await assign(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )
        toggle = SetModEnabled(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await toggle(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_id=mod.id,
            enabled=False,
        )

        assignment = await toggle(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_id=mod.id,
            enabled=True,
        )

        assert assignment.enabled is True
        assert file_store.files["mods/sodium.jar"] == b"JAR!"
        assert "mods/sodium.jar.disabled" not in file_store.files

    async def test_toggle_client_only_updates_row_without_deploying(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod(side="client"))

        assign = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await assign(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )

        uc = SetModEnabled(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        assignment = await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_id=mod.id,
            enabled=False,
        )

        assert assignment.enabled is False
        assert file_store.files == {}

    async def test_toggle_rejects_unassigned(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        uc = SetModEnabled(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        with pytest.raises(ModAssignmentNotFoundError):
            await uc(
                community_id=_COMMUNITY_ID,
                server_id=server.id,
                mod_id=mod.id,
                enabled=False,
            )

    async def test_toggle_rejects_running_server(self) -> None:
        server = _running_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        uc = SetModEnabled(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        with pytest.raises(ServerFilesUnsettledError):
            await uc(
                community_id=_COMMUNITY_ID,
                server_id=server.id,
                mod_id=mod.id,
                enabled=True,
            )


class TestListServerMods:
    async def test_list_returns_assignments_with_mods(self) -> None:
        server = _at_rest_server()
        uow, file_store, store = _ctx(server)
        mod = _seed_mod(uow, store, _make_mod())

        assign = AssignMods(
            uow=uow, file_store=file_store, store=store, clock=FakeClock(_NOW)
        )
        await assign(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            mod_ids=[mod.id],
            assigned_by=uuid.uuid4(),
        )

        uc = ListServerMods(uow=uow)
        result = await uc(community_id=_COMMUNITY_ID, server_id=server.id)

        assert len(result.entries) == 1
        assignment, returned_mod = result.entries[0]
        assert assignment.mod_id == mod.id
        assert returned_mod.id == mod.id

    async def test_list_empty(self) -> None:
        server = _at_rest_server()
        uow, _, _ = _ctx(server)
        uc = ListServerMods(uow=uow)
        result = await uc(community_id=_COMMUNITY_ID, server_id=server.id)
        assert result.entries == []

    async def test_list_rejects_unknown_server(self) -> None:
        uow = FakeUnitOfWork()
        uc = ListServerMods(uow=uow)
        with pytest.raises(ServerNotFoundError):
            await uc(community_id=_COMMUNITY_ID, server_id=ServerId(uuid.uuid4()))


def _seed_assigned_mod(
    uow: FakeUnitOfWork,
    store: FakeModStore,
    server_id: ServerId,
    mod: Mod,
    *,
    enabled: bool = True,
    blob: bytes = b"JAR!",
) -> Mod:
    """Seed a library mod, its jar bytes, and an assignment row to a server."""

    from mc_server_dashboard_api.servers.domain.server_mod import (
        ServerModAssignment,
        ServerModId,
    )

    uow.mods.mods[mod.id] = mod
    store.blobs[mod.id] = blob
    assignment = ServerModAssignment(
        id=ServerModId.new(),
        server_id=server_id,
        mod_id=mod.id,
        enabled=enabled,
        assigned_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    uow.mods.assignments[assignment.id] = assignment
    return mod


class TestListClientMods:
    async def test_includes_client_and_both_excludes_server(self) -> None:
        server = _at_rest_server()
        uow, _, store = _ctx(server)
        client = _seed_assigned_mod(
            uow, store, server.id, _make_mod(side="client", filename="minimap.jar")
        )
        both = _seed_assigned_mod(
            uow, store, server.id, _make_mod(side="both", filename="fabric-api.jar")
        )
        _seed_assigned_mod(
            uow, store, server.id, _make_mod(side="server", filename="lithium.jar")
        )

        uc = ListClientMods(uow=uow)
        result = await uc(community_id=_COMMUNITY_ID, server_id=server.id)

        assert {m.id for m in result} == {client.id, both.id}

    async def test_excludes_disabled(self) -> None:
        server = _at_rest_server()
        uow, _, store = _ctx(server)
        enabled = _seed_assigned_mod(
            uow, store, server.id, _make_mod(side="both", filename="enabled.jar")
        )
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="client", filename="disabled.jar"),
            enabled=False,
        )

        uc = ListClientMods(uow=uow)
        result = await uc(community_id=_COMMUNITY_ID, server_id=server.id)

        assert [m.id for m in result] == [enabled.id]

    async def test_empty_when_no_client_mods(self) -> None:
        server = _at_rest_server()
        uow, _, store = _ctx(server)
        _seed_assigned_mod(uow, store, server.id, _make_mod(side="server"))

        uc = ListClientMods(uow=uow)
        result = await uc(community_id=_COMMUNITY_ID, server_id=server.id)

        assert result == []

    async def test_rejects_unknown_server(self) -> None:
        uow = FakeUnitOfWork()
        uc = ListClientMods(uow=uow)
        with pytest.raises(ServerNotFoundError):
            await uc(community_id=_COMMUNITY_ID, server_id=ServerId(uuid.uuid4()))


def _read_zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


async def _collect(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


class TestDownloadClientModpack:
    async def test_zip_contains_exactly_client_jars_with_bytes(self) -> None:
        server = _at_rest_server()
        uow, _, store = _ctx(server)
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="both", filename="fabric-api.jar"),
            blob=b"FABRIC-API-BYTES",
        )
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="client", filename="minimap.jar"),
            blob=b"MINIMAP-BYTES",
        )
        # Server-only mod is excluded from the client modpack.
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="server", filename="lithium.jar"),
            blob=b"LITHIUM",
        )

        uc = DownloadClientModpack(uow=uow, store=store)
        stream, name = await uc(community_id=_COMMUNITY_ID, server_id=server.id)
        entries = _read_zip(await _collect(stream))

        assert name == "test-server"
        assert entries == {
            "fabric-api.jar": b"FABRIC-API-BYTES",
            "minimap.jar": b"MINIMAP-BYTES",
        }

    async def test_excludes_disabled_from_zip(self) -> None:
        server = _at_rest_server()
        uow, _, store = _ctx(server)
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="both", filename="enabled.jar"),
            blob=b"ENABLED",
        )
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="client", filename="disabled.jar"),
            enabled=False,
            blob=b"DISABLED",
        )

        uc = DownloadClientModpack(uow=uow, store=store)
        stream, _ = await uc(community_id=_COMMUNITY_ID, server_id=server.id)
        entries = _read_zip(await _collect(stream))

        assert entries == {"enabled.jar": b"ENABLED"}

    async def test_duplicate_filenames_do_not_corrupt_zip(self) -> None:
        server = _at_rest_server()
        uow, _, store = _ctx(server)
        # Two distinct library mods that happen to share a filename.
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="both", filename="dup.jar", sha256="1" * 64),
            blob=b"FIRST",
        )
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="both", filename="dup.jar", sha256="2" * 64),
            blob=b"SECOND",
        )

        uc = DownloadClientModpack(uow=uow, store=store)
        stream, _ = await uc(community_id=_COMMUNITY_ID, server_id=server.id)
        entries = _read_zip(await _collect(stream))

        # Both jars are present under distinct, non-colliding entry names.
        assert len(entries) == 2
        assert "dup.jar" in entries
        assert set(entries.values()) == {b"FIRST", b"SECOND"}

    async def test_empty_zip_when_no_client_mods(self) -> None:
        server = _at_rest_server()
        uow, _, store = _ctx(server)
        _seed_assigned_mod(uow, store, server.id, _make_mod(side="server"))

        uc = DownloadClientModpack(uow=uow, store=store)
        stream, _ = await uc(community_id=_COMMUNITY_ID, server_id=server.id)
        entries = _read_zip(await _collect(stream))

        assert entries == {}

    async def test_large_multi_chunk_jars_produce_valid_archive(self) -> None:
        # Jars larger than the internal drain threshold, streamed from the store
        # in many chunks, must still produce a structurally valid zip with each
        # entry's bytes preserved exactly (issue #1265 corrupt-archive fix).
        server = _at_rest_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        store = FakeModStore(chunk_size=64 * 1024)

        # Deterministic but incompressible: deflate cannot shrink it below the
        # drain threshold, so the streaming/drain path genuinely executes.
        rng = random.Random(1265)
        big_a = rng.randbytes(3 * 1024 * 1024)
        big_b = rng.randbytes(5 * 1024 * 1024)
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="both", filename="big-a.jar"),
            blob=big_a,
        )
        _seed_assigned_mod(
            uow,
            store,
            server.id,
            _make_mod(side="client", filename="big-b.jar", sha256="b" * 64),
            blob=big_b,
        )

        uc = DownloadClientModpack(uow=uow, store=store)
        stream, _ = await uc(community_id=_COMMUNITY_ID, server_id=server.id)
        data = await _collect(stream)

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.testzip() is None
            assert set(zf.namelist()) == {"big-a.jar", "big-b.jar"}
            assert zf.read("big-a.jar") == big_a
            assert zf.read("big-b.jar") == big_b

    async def test_rejects_unknown_server(self) -> None:
        uow = FakeUnitOfWork()
        uc = DownloadClientModpack(uow=uow, store=FakeModStore())
        with pytest.raises(ServerNotFoundError):
            await uc(community_id=_COMMUNITY_ID, server_id=ServerId(uuid.uuid4()))
