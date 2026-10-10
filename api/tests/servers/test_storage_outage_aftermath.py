"""What a store outage leaves behind a use case, and what a repeat then does (#3233).

The seam-level aftermath is pinned in ``test_file_store_storage_unavailable.py``.
These tests add the half a route's status depends on that the seam cannot show:
what the USE CASE had already committed when the outage struck, and what the
same request answers when it is sent again. Each drives the real use case over
the real seam and the real :class:`ObjectStorage` (in-memory S3 stub, outage
injected at a chosen call); only the database is a fake.

- Where the repeat completes the operation, the route answers 503
  ``storage_unavailable``: file write / upload / archive extract, a start's EULA
  read and ``accept_eula`` write, a resource pack assign's properties read and an
  unassign's read and write, and a plugin install's cache ingest.
- Where it does not — the row is already committed, or the mutation already
  landed — no route maps the outage and the edge keeps its 500: file delete and
  rename, and the working-set write that follows a plugin install's or removal's
  commit.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
import zipfile
from typing import Any

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import (
    StorageFileStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.plugin_cache_store import (
    ObjectPluginCacheStore,
)
from mc_server_dashboard_api.servers.adapters.store_generation import (
    StorageGenerationReader,
)
from mc_server_dashboard_api.servers.application.client_modpack import (
    DownloadClientModpack,
)
from mc_server_dashboard_api.servers.application.files import (
    DeleteFile,
    RenameFile,
    UploadFile,
)
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.application.plugins import (
    InstallPlugin,
    RemovePlugin,
)
from mc_server_dashboard_api.servers.application.resource_packs import (
    AssignResourcePack,
    UnassignResourcePack,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileAlreadyExistsError,
    PluginAlreadyExistsError,
    PluginCacheStorageUnavailableError,
    PluginNotFoundError,
    ServerFileNotFoundError,
    ServerFileStorageUnavailableError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
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
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeJarProvisioner,
    FakeUnitOfWork,
)
from tests.storage.fake_s3 import FakeS3Store
from tests.storage.faulty_s3 import Faults, faulty_s3_factory

_NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())
_PROPERTIES = b"motd=hi\nresource-pack=url\nresource-pack-sha1=sha\n"


def _generation_bump(op: str, key: str) -> bool:
    """The last step of every authoritative edit: rewriting the generation marker."""

    return op == "put_object" and key.endswith("/generation")


def _working_set_put(name: str) -> Any:
    """The step that replaces ``name`` in the live working set."""

    return lambda op, key: (
        op == "put_object" and "/snapshots/" in key and key.endswith("/" + name)
    )


class _Rig:
    """An at-rest server whose working set and jar cache sit on a faultable store."""

    def __init__(self, *, server_type: ServerType = ServerType.VANILLA) -> None:
        self.faults = Faults()
        factory = faulty_s3_factory(FakeS3Store(), self.faults)
        self.storage = ObjectStorage(factory)
        self.files = StorageFileStoreAdapter(storage=self.storage)
        self.cache = ObjectPluginCacheStore(factory)
        self.uow = FakeUnitOfWork()
        self.server = Server(
            id=ServerId.new(),
            community_id=_COMMUNITY,
            name=ServerName("survival"),
            mc_edition="java",
            mc_version="1.21.1",
            server_type=server_type,
            config={},
            desired_state=DesiredState.STOPPED,
            observed_state=ObservedState.STOPPED,
            observed_at=_NOW,
            assigned_worker_id=None,
            created_at=_NOW,
            updated_at=_NOW,
        )
        self.uow.servers.seed(self.server)

    @property
    def scope(self) -> dict[str, Any]:
        return {"community_id": _COMMUNITY, "server_id": self.server.id}

    async def seed(self, files: dict[str, bytes]) -> None:
        for rel_path, content in files.items():
            await self.files.write_file(
                **self.scope, rel_path=rel_path, content=content
            )

    async def read(self, rel_path: str) -> bytes:
        return await self.files.read_file(**self.scope, rel_path=rel_path)

    async def generation(self) -> int:
        reader = StorageGenerationReader(storage=self.storage)
        return await reader.current_generation(**self.scope)


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


# --- file routes -----------------------------------------------------------


async def test_archive_extract_interrupted_partway_is_finished_by_a_repeat() -> None:
    """An extract writes its members one by one, so an outage leaves the earlier
    ones in place. Repeating the upload rewrites every member with the same
    bytes, which is the state a first-time success would have left."""

    rig = _Rig()
    await rig.seed({"eula.txt": b"eula=true\n"})
    upload = UploadFile(uow=rig.uow, file_store=rig.files)
    archive = _zip({"a.txt": b"first", "b.txt": b"second"})
    rig.faults.when = _working_set_put("b.txt")

    async def _extract() -> None:
        await upload(
            **rig.scope,
            dir_path="config",
            filename="pack.zip",
            content=archive,
            extract=True,
        )

    with pytest.raises(ServerFileStorageUnavailableError):
        await _extract()

    rig.faults.clear()
    assert await rig.read("config/a.txt") == b"first"
    with pytest.raises(ServerFileNotFoundError):
        await rig.read("config/b.txt")

    await _extract()

    assert await rig.read("config/a.txt") == b"first"
    assert await rig.read("config/b.txt") == b"second"


async def test_delete_interrupted_after_its_mutation_answers_a_repeat_not_found() -> (
    None
):
    """Why DELETE keeps its 500: the file is gone when the outage is reported, so
    the repeat is a 404, and the generation the interrupted delete never bumped
    stays unbumped."""

    rig = _Rig()
    await rig.seed({"notes.txt": b"x"})
    before = await rig.generation()
    delete = DeleteFile(uow=rig.uow, file_store=rig.files)
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await delete(**rig.scope, rel_path="notes.txt")

    rig.faults.clear()
    with pytest.raises(ServerFileNotFoundError):
        await delete(**rig.scope, rel_path="notes.txt")
    assert await rig.generation() == before


async def test_rename_interrupted_after_its_copy_answers_a_repeat_conflict() -> None:
    """Why rename keeps its 500: both names exist when the outage is reported, and
    the repeat is refused as a never-clobber conflict instead of finishing."""

    rig = _Rig()
    await rig.seed({"notes.txt": b"x"})
    rename = RenameFile(uow=rig.uow, file_store=rig.files)
    rig.faults.when = lambda op, key: op == "delete_object"

    with pytest.raises(ServerFileStorageUnavailableError):
        await rename(**rig.scope, from_path="notes.txt", to_path="renamed.txt")

    rig.faults.clear()
    with pytest.raises(FileAlreadyExistsError):
        await rename(**rig.scope, from_path="notes.txt", to_path="renamed.txt")


# --- start -----------------------------------------------------------------


def _start(rig: _Rig, control_plane: FakeControlPlane) -> StartServer:
    return StartServer(
        uow=rig.uow,
        control_plane=control_plane,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=StorageGenerationReader(storage=rig.storage),
        file_store=rig.files,
    )


async def _assert_not_started(rig: _Rig, control_plane: FakeControlPlane) -> None:
    stored = await rig.uow.servers.get_by_id(rig.server.id)
    assert stored is not None
    assert stored.desired_state is DesiredState.STOPPED
    assert stored.assigned_worker_id is None
    assert control_plane.dispatched == []


async def test_start_whose_eula_read_hits_an_outage_starts_nothing() -> None:
    rig = _Rig()
    await rig.seed({"eula.txt": b"eula=true\n"})
    control_plane = FakeControlPlane(place_to=WorkerId(uuid.uuid4()))
    rig.faults.always()

    with pytest.raises(ServerFileStorageUnavailableError):
        await _start(rig, control_plane)(**rig.scope)

    rig.faults.clear()
    await _assert_not_started(rig, control_plane)


async def test_start_whose_eula_write_hits_an_outage_is_finished_by_a_repeat() -> None:
    rig = _Rig()
    await rig.seed({"server.properties": b"motd=hi\n"})
    worker = WorkerId(uuid.uuid4())
    control_plane = FakeControlPlane(place_to=worker)
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await _start(rig, control_plane)(**rig.scope, accept_eula=True)

    rig.faults.clear()
    await _assert_not_started(rig, control_plane)

    await _start(rig, control_plane)(**rig.scope, accept_eula=True)

    assert await rig.read("eula.txt") == b"eula=true\n"
    assert control_plane.dispatched[-1] == ("start", worker, rig.server.id)


# --- resource pack assignment ----------------------------------------------


def _seed_pack(rig: _Rig) -> ResourcePack:
    pack = ResourcePack(
        id=ResourcePackId.new(),
        filename="pack.zip",
        display_name="Pack",
        description=None,
        sha1_hash="abc123",
        sha256_hash="def456",
        size_bytes=1234,
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    rig.uow.resource_packs.packs[pack.id] = pack
    return pack


async def _seed_assignment(rig: _Rig) -> None:
    pack = _seed_pack(rig)
    await rig.uow.resource_packs.add_assignment(
        ResourcePackAssignment(
            server_id=rig.server.id,
            resource_pack_id=pack.id,
            require_resource_pack=True,
            resource_pack_prompt=None,
            assigned_by=uuid.uuid4(),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


async def test_assign_whose_properties_read_hits_an_outage_assigns_nothing() -> None:
    rig = _Rig()
    await rig.seed({"server.properties": b"motd=hi\n"})
    pack = _seed_pack(rig)
    assign = AssignResourcePack(
        uow=rig.uow, file_store=rig.files, clock=FakeClock(_NOW)
    )
    rig.faults.always()

    with pytest.raises(ServerFileStorageUnavailableError):
        await assign(
            **rig.scope,
            resource_pack_id=pack.id,
            require_resource_pack=False,
            resource_pack_prompt=None,
            assigned_by=uuid.uuid4(),
            public_base_url="https://example.com",
        )

    rig.faults.clear()
    assert await rig.uow.resource_packs.get_assignment_by_server(rig.server.id) is None
    assert await rig.read("server.properties") == b"motd=hi\n"


async def test_unassign_whose_properties_read_hits_an_outage_keeps_the_row() -> None:
    rig = _Rig()
    await rig.seed({"server.properties": _PROPERTIES})
    await _seed_assignment(rig)
    unassign = UnassignResourcePack(uow=rig.uow, file_store=rig.files)
    rig.faults.always()

    with pytest.raises(ServerFileStorageUnavailableError):
        await unassign(**rig.scope)

    rig.faults.clear()
    assert await rig.uow.resource_packs.get_assignment_by_server(rig.server.id)
    assert await rig.read("server.properties") == _PROPERTIES


async def test_unassign_whose_write_hits_an_outage_is_finished_by_a_repeat() -> None:
    """The file is rewritten before the row is deleted, so an outage on the write
    leaves the assignment standing — with the file possibly already cleared — and
    unassigning again clears the same keys and then removes the row."""

    rig = _Rig()
    await rig.seed({"server.properties": _PROPERTIES})
    await _seed_assignment(rig)
    unassign = UnassignResourcePack(uow=rig.uow, file_store=rig.files)
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await unassign(**rig.scope)

    rig.faults.clear()
    assert await rig.uow.resource_packs.get_assignment_by_server(rig.server.id)

    await unassign(**rig.scope)

    assert await rig.uow.resource_packs.get_assignment_by_server(rig.server.id) is None
    assert b"resource-pack=url" not in await rig.read("server.properties")


# --- plugins ---------------------------------------------------------------


def _install(rig: _Rig) -> InstallPlugin:
    return InstallPlugin(
        uow=rig.uow, file_store=rig.files, cache=rig.cache, clock=FakeClock(_NOW)
    )


async def _install_mod(rig: _Rig) -> Any:
    return await _install(rig)(
        **rig.scope, filename="mod.jar", display_name="Mod", content=b"jar-bytes"
    )


async def test_install_whose_cache_ingest_hits_an_outage_is_finished_by_a_repeat() -> (
    None
):
    """The jar is ingested into the cache before the plugin row is added, so an
    outage there commits nothing and the repeat installs normally."""

    rig = _Rig(server_type=ServerType.FABRIC)
    await rig.seed({"eula.txt": b"eula=true\n"})
    rig.faults.when = lambda op, key: key.startswith("plugin-cache/")

    with pytest.raises(PluginCacheStorageUnavailableError):
        await _install_mod(rig)

    rig.faults.clear()
    assert await rig.uow.plugins.list_for_server(rig.server.id) == []

    plugin = await _install_mod(rig)

    assert await rig.read(plugin.rel_path) == b"jar-bytes"


async def test_install_whose_working_set_write_hits_an_outage_cannot_be_repeated() -> (
    None
):
    """Why the install keeps its 500 for THIS outage: the jar is written after
    the row commits, so the row is in and the jar is not, and installing again
    is refused as a duplicate."""

    rig = _Rig(server_type=ServerType.FABRIC)
    await rig.seed({"eula.txt": b"eula=true\n"})
    rig.faults.when = _working_set_put("mod.jar")

    with pytest.raises(ServerFileStorageUnavailableError):
        await _install_mod(rig)

    rig.faults.clear()
    assert len(await rig.uow.plugins.list_for_server(rig.server.id)) == 1
    with pytest.raises(PluginAlreadyExistsError):
        await _install_mod(rig)


async def test_remove_whose_working_set_delete_hits_an_outage_cannot_be_repeated() -> (
    None
):
    """Why the removal keeps its 500: the row is deleted before the jar, so the
    repeat finds no plugin while the jar is still in the working set."""

    rig = _Rig(server_type=ServerType.FABRIC)
    await rig.seed({"eula.txt": b"eula=true\n"})
    plugin = await _install_mod(rig)
    remove = RemovePlugin(uow=rig.uow, file_store=rig.files, clock=FakeClock(_NOW))
    rig.faults.always()

    with pytest.raises(ServerFileStorageUnavailableError):
        await remove(**rig.scope, plugin_id=plugin.id)

    rig.faults.clear()
    with pytest.raises(PluginNotFoundError):
        await remove(**rig.scope, plugin_id=plugin.id)
    assert await rig.read(plugin.rel_path) == b"jar-bytes"


# --- the same writes with the file's version ring full ----------------------

# The adapter's default retention. A full ring evicts its oldest entry on every
# further capture — the state the rollback's repeat does not survive (pinned at
# the seam), re-examined here for each write whose route answers 503.
_RING = 10


async def _fill_ring(rig: _Rig, rel_path: str) -> None:
    for n in range(_RING + 1):
        await rig.files.write_file(
            **rig.scope, rel_path=rel_path, content=b"old-%d" % n
        )
    assert len(await rig.files.list_versions(**rig.scope, rel_path=rel_path)) == _RING


async def test_archive_extract_over_full_rings_is_still_finished_by_a_repeat() -> None:
    """An upload's bytes come from the request, so no eviction can take them
    away: the repeat converges with every member's ring full."""

    rig = _Rig()
    await _fill_ring(rig, "config/a.txt")
    await _fill_ring(rig, "config/b.txt")
    before = await rig.generation()
    upload = UploadFile(uow=rig.uow, file_store=rig.files)
    archive = _zip({"a.txt": b"first", "b.txt": b"second"})

    async def _extract() -> None:
        await upload(
            **rig.scope,
            dir_path="config",
            filename="pack.zip",
            content=archive,
            extract=True,
        )

    # The first member lands whole; the second is replaced but its generation
    # bump is not made.
    bumps = 0

    def _second_bump(op: str, key: str) -> bool:
        nonlocal bumps
        if _generation_bump(op, key):
            bumps += 1
            return bumps == 2
        return False

    rig.faults.when = _second_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await _extract()

    rig.faults.clear()
    await _extract()

    assert await rig.read("config/a.txt") == b"first"
    assert await rig.read("config/b.txt") == b"second"
    # One bump from the interrupted attempt's first member, two from the repeat.
    assert await rig.generation() == before + 3


async def test_start_eula_write_over_a_full_ring_is_still_finished_by_a_repeat() -> (
    None
):
    rig = _Rig()
    await rig.seed({"server.properties": b"motd=hi\n"})
    await _fill_ring(rig, "eula.txt")
    worker = WorkerId(uuid.uuid4())
    control_plane = FakeControlPlane(place_to=worker)
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await _start(rig, control_plane)(**rig.scope, accept_eula=True)

    rig.faults.clear()
    await _assert_not_started(rig, control_plane)

    await _start(rig, control_plane)(**rig.scope, accept_eula=True)

    assert await rig.read("eula.txt") == b"eula=true\n"
    assert control_plane.dispatched[-1] == ("start", worker, rig.server.id)


# --- a closed client-mods download holds nothing (issue #3234) --------------


async def test_closing_a_begun_client_modpack_releases_the_cache_client() -> None:
    """Closed while a jar is open: the zip closes that jar's cache stream, so no
    store client stays open behind it."""

    rig = _Rig(server_type=ServerType.FABRIC)
    await rig.seed({"eula.txt": b"eula=true\n"})
    plugin = await _install_mod(rig)
    plugin.side = "client"
    await rig.uow.plugins.update(plugin)
    download = DownloadClientModpack(uow=rig.uow, cache=rig.cache)
    stream = await download(**rig.scope)
    await anext(stream)
    assert rig.faults.open_clients == 1

    await stream.aclose()  # type: ignore[attr-defined]

    assert rig.faults.open_clients == 0
