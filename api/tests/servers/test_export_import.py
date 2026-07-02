"""Use-case tests for whole-server ZIP export / import (issue #274).

Exercises :mod:`servers.application.export_import` against the servers fakes (no
DB, no real Storage), per TESTING.md Section 4. Verifies:

- a round-trip: export server A's working set + metadata, then import it as a
  fresh server B, and the published working set + metadata-driven fields match;
- export is at-rest only (running -> 409 via ServerFilesUnsettledError) and
  ``exported_at`` comes from the Clock seam;
- import validation: missing/wrong-format/malformed metadata -> 422; the name comes
  from the request (uniqueness 409); the row gets an auto-assigned game port;
- import caps: an oversized body / over-cap extraction -> 413;
- import failure posture: a storage write failure mid-publish -> the seed-failure
  503 posture, with the row already created;
- import Bedrock enablement (issue #1551): a re-created Geyser plugin allocates
  the imported server's ``bedrock_port`` when the deployment gate is on, leaves it
  unset when the gate is off, a non-Geyser plugin never allocates, and a Bedrock
  window exhaustion or the UNIQUE(bedrock_port) racer backstop surfaces the same
  seed-failure 503 posture (never an unmapped 500).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.application.export_import import (
    EXPORT_FORMAT_VERSION,
    EXPORT_METADATA_FILENAME,
    ExportServer,
    ImportServer,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidExportMetadataError,
    InvalidFilePathError,
    PortAlreadyTakenError,
    ServerFilesUnsettledError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeServerRepository,
    FakeUnitOfWork,
    FakeVersionValidator,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_PORT_RANGE = PortRange(start=25565, end=25600)
_BEDROCK_RANGE = PortRange(start=19132, end=19141)


def _server(*, community_id: uuid.UUID, server_id: uuid.UUID) -> Server:
    return Server(
        id=ServerId(server_id),
        community_id=CommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        game_port=25565,
    )


def _create_server(uow: FakeUnitOfWork, store: FakeFileStore) -> CreateServer:
    return CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=store,
        port_range=_PORT_RANGE,
    )


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _metadata(
    *, server_type: str = "vanilla", mc_version: str = "1.21.1", **overrides: object
) -> bytes:
    body: dict[str, object] = {
        "format": EXPORT_FORMAT_VERSION,
        "name": "exported-name",
        "mc_edition": "java",
        "mc_version": mc_version,
        "server_type": server_type,
        "exported_at": _NOW.isoformat(),
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


async def _drain(stream: AsyncIterator[bytes]) -> bytes:
    out = bytearray()
    async for chunk in stream:
        out += chunk
    return bytes(out)


# --- export ----------------------------------------------------------------


async def test_export_streams_working_set_plus_metadata() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    store = FakeFileStore()
    store.files["server.properties"] = b"motd=hi"
    store.files["world/level.dat"] = b"\x00\x01"
    use_case = ExportServer(uow=uow, clock=FakeClock(_NOW), file_store=store)

    stream = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    data = await _drain(stream)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert "server.properties" in names
        assert "world/level.dat" in names
        assert EXPORT_METADATA_FILENAME in names
        meta = json.loads(zf.read(EXPORT_METADATA_FILENAME))
    assert meta["format"] == EXPORT_FORMAT_VERSION
    assert meta["server_type"] == "vanilla"
    assert meta["mc_version"] == "1.21.1"
    # ``exported_at`` is the canonical RFC 3339 ``Z`` form (#674), not the
    # ``+00:00`` offset that ``datetime.isoformat()`` would emit for UTC.
    assert meta["exported_at"] == "2026-06-04T12:00:00Z"


async def test_export_running_is_unsettled() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    running = _server(community_id=community, server_id=server_id)
    running.desired_state = DesiredState.RUNNING
    running.observed_state = ObservedState.RUNNING
    from mc_server_dashboard_api.servers.domain.value_objects import WorkerId

    running.assigned_worker_id = WorkerId(worker)
    uow.servers.seed(running)
    use_case = ExportServer(uow=uow, clock=FakeClock(_NOW), file_store=FakeFileStore())

    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )


# --- round-trip ------------------------------------------------------------


async def test_export_then_import_round_trips_files_and_metadata() -> None:
    community = uuid.uuid4()
    src_id = uuid.uuid4()

    src_uow = FakeUnitOfWork()
    src_uow.servers.seed(_server(community_id=community, server_id=src_id))
    src_store = FakeFileStore()
    src_store.files["server.properties"] = b"motd=hi"
    src_store.files["world/level.dat"] = b"\x00\x01\x02"
    export = ExportServer(uow=src_uow, clock=FakeClock(_NOW), file_store=src_store)
    archive = await _drain(
        await export(community_id=CommunityId(community), server_id=ServerId(src_id))
    )

    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )

    # The metadata-driven fields match the source; the name comes from the request.
    assert server.name.value == "imported"
    assert server.mc_version == "1.21.1"
    assert server.server_type is ServerType.VANILLA
    assert server.game_port is not None
    # The working set is published identically except that import enforces the RCON
    # keys on server.properties (#335); the metadata file is excluded.
    props = dst_store.files["server.properties"].decode()
    assert "motd=hi" in props
    assert "enable-rcon=true" in props
    assert "rcon.port=25575" in props
    assert dst_store.files["world/level.dat"] == b"\x00\x01\x02"
    assert EXPORT_METADATA_FILENAME not in dst_store.files


# --- import: RCON enforcement (issue #335) ---------------------------------


async def test_import_enforces_rcon_on_disabled_properties() -> None:
    # An archive whose server.properties has RCON off / a stray port gets RCON
    # enabled and the port corrected, with a generated password filled in.
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "server.properties": b"enable-rcon=false\nrcon.port=1234\nmotd=hi\n",
        }
    )
    await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    props = dict(
        line.split("=", 1)
        for line in dst_store.files["server.properties"].decode().splitlines()
        if "=" in line
    )
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == "25575"
    assert props["rcon.password"] != ""
    assert props["motd"] == "hi"


async def test_import_preserves_existing_non_empty_rcon_password() -> None:
    # A non-empty existing rcon.password is kept (an importer's known credential),
    # while enable-rcon / rcon.port are still enforced.
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "server.properties": (
                b"enable-rcon=false\nrcon.password=known-secret\nrcon.port=1234\n"
            ),
        }
    )
    await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    props = dict(
        line.split("=", 1)
        for line in dst_store.files["server.properties"].decode().splitlines()
        if "=" in line
    )
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == "25575"
    assert props["rcon.password"] == "known-secret"


async def test_import_seeds_rcon_when_archive_has_no_properties() -> None:
    # An archive with no server.properties at all still ends up with one carrying
    # the enforced RCON keys, so the imported server's console works out of the box.
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), "world/level.dat": b"\x00"})
    await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    props = dict(
        line.split("=", 1)
        for line in dst_store.files["server.properties"].decode().splitlines()
        if "=" in line
    )
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == "25575"
    assert props["rcon.password"] != ""


# --- import validation -----------------------------------------------------


async def test_import_rejects_non_zip_body() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            content=b"not a zip",
        )


async def test_import_rejects_missing_metadata() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            content=_zip({"server.properties": b"motd=hi"}),
        )


async def test_import_rejects_wrong_format_version() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    bad = _zip({EXPORT_METADATA_FILENAME: _metadata(format=99)})
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            content=bad,
        )


async def test_import_rejects_malformed_json() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    bad = _zip({EXPORT_METADATA_FILENAME: b"{not json"})
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            content=bad,
        )


async def test_import_assigns_game_port() -> None:
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata()})
    server = await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    assert _PORT_RANGE.start <= server.game_port <= _PORT_RANGE.end  # type: ignore[operator]


async def test_import_oversized_is_too_large() -> None:
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "world/region.bin": b"x" * 64,
        }
    )
    # Tiny caps trip the cumulative-size guard during extraction with a small body.
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        max_bytes=16,
        max_entries=100,
    )
    with pytest.raises(FileTooLargeError):
        await imp(
            community_id=CommunityId(community),
            name="fresh",
            content=archive,
        )
    # The validate-first pass (#277) rejects the hostile archive BEFORE the row is
    # created: a 413 leaves no server row behind, unlike the seed-failure posture.
    assert len(dst_uow.servers.by_id) == 0


async def test_import_zip_slip_entry_creates_no_row() -> None:
    # A zip-slip member (422) is a property of the archive and is rejected by the
    # pre-commit validate pass, so no server row is created (#277).
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), "../escape.txt": b"pwned"})
    with pytest.raises(InvalidFilePathError):
        await imp(
            community_id=CommunityId(community),
            name="fresh",
            content=archive,
        )
    assert len(dst_uow.servers.by_id) == 0
    assert dst_store.files == {}


async def test_import_publish_failure_is_seed_failed() -> None:
    community = uuid.uuid4()
    dst_uow = FakeUnitOfWork()

    class _FailWorldStore(FakeFileStore):
        # Let create's own seeding (server.properties / eula.txt) succeed, but fail
        # the import publish of a working-set file, so the failure exercises the
        # import publish posture rather than create's seeding posture.
        async def write_file(self, *, community_id, server_id, rel_path, content):  # type: ignore[no-untyped-def]
            if rel_path.startswith("world/"):
                raise RuntimeError("forced storage write failure")
            await super().write_file(
                community_id=community_id,
                server_id=server_id,
                rel_path=rel_path,
                content=content,
            )

    failing_store = _FailWorldStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, failing_store),
        file_store=failing_store,
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), "world/level.dat": b"\x00"})
    with pytest.raises(WorkingSetSeedFailedError):
        await imp(
            community_id=CommunityId(community),
            name="fresh",
            content=archive,
        )
    # The row was created before the publish failed (degraded but repairable).
    assert len(dst_uow.servers.by_id) == 1


# --- plugin metadata in export/import (issue #1335) --------------------------


def _plugin(
    *, server_id: uuid.UUID, mod_identifier: str = "fabric-api"
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=ServerId(server_id),
        rel_path=f"mods/{mod_identifier}.jar",
        filename=f"{mod_identifier}.jar",
        display_name=mod_identifier.title(),
        description="A test plugin",
        loader_type=LoaderType.MOD,
        source=PluginSource.MODRINTH,
        source_project_id="proj-1",
        source_version_id="ver-1",
        version_number="1.0.0",
        checksum_sha512="abc123",
        sha256="def456",
        size_bytes=1024,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        mod_identifier=mod_identifier,
        provides=["fabric-api-base"],
        dependencies=[
            {
                "mod_identifier": "minecraft",
                "version_range": ">=1.21",
                "required": True,
                "conflict": False,
            }
        ],
        mc_versions=["1.21.1"],
        side="both",
        catalog_dependencies=[
            {
                "project_id": "P7dR8mSH",
                "required": True,
                "slug": "fabric-api",
                "title": "Fabric API",
            }
        ],
    )


async def test_export_includes_plugin_metadata() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    plugin = _plugin(server_id=server_id)
    uow.plugins.seed(plugin)
    store = FakeFileStore()
    store.files["mods/fabric-api.jar"] = b"\x00jar"
    use_case = ExportServer(uow=uow, clock=FakeClock(_NOW), file_store=store)

    stream = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    data = await _drain(stream)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        meta = json.loads(zf.read(EXPORT_METADATA_FILENAME))
    assert "plugins" in meta
    assert len(meta["plugins"]) == 1
    p = meta["plugins"][0]
    assert p["mod_identifier"] == "fabric-api"
    assert p["rel_path"] == "mods/fabric-api.jar"
    assert p["loader_type"] == "mod"
    assert p["source"] == "modrinth"
    assert p["side"] == "both"
    assert p["provides"] == ["fabric-api-base"]
    assert p["catalog_dependencies"] == plugin.catalog_dependencies


async def test_export_no_plugins_has_empty_list() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    store = FakeFileStore()
    store.files["server.properties"] = b"motd=hi"
    use_case = ExportServer(uow=uow, clock=FakeClock(_NOW), file_store=store)

    stream = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    data = await _drain(stream)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        meta = json.loads(zf.read(EXPORT_METADATA_FILENAME))
    assert meta["plugins"] == []


async def test_import_recreates_plugin_records_from_metadata() -> None:
    community = uuid.uuid4()
    src_id = uuid.uuid4()

    # Build an export archive from a server that has a plugin.
    src_uow = FakeUnitOfWork()
    src_uow.servers.seed(_server(community_id=community, server_id=src_id))
    plugin = _plugin(server_id=src_id)
    src_uow.plugins.seed(plugin)
    src_store = FakeFileStore()
    src_store.files["mods/fabric-api.jar"] = b"\x00jar"
    export = ExportServer(uow=src_uow, clock=FakeClock(_NOW), file_store=src_store)
    archive = await _drain(
        await export(community_id=CommunityId(community), server_id=ServerId(src_id))
    )

    # Import the archive.
    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )

    # The imported server should have the plugin record.
    imported_plugins = await dst_uow.plugins.list_for_server(server.id)
    assert len(imported_plugins) == 1
    p = imported_plugins[0]
    assert p.mod_identifier == "fabric-api"
    assert p.rel_path == "mods/fabric-api.jar"
    assert p.display_name == plugin.display_name
    assert p.loader_type is LoaderType.MOD
    assert p.source is PluginSource.MODRINTH
    assert p.source_project_id == "proj-1"
    assert p.provides == ["fabric-api-base"]
    assert p.side == "both"
    assert p.catalog_dependencies == plugin.catalog_dependencies
    assert p.dependencies == plugin.dependencies
    # The id should be fresh (not the source server's id).
    assert p.id != plugin.id
    assert p.server_id == server.id


async def test_import_old_archive_without_plugins_succeeds() -> None:
    # An archive from before #1335 has no "plugins" key; import proceeds
    # without error and has no plugin rows.
    community = uuid.uuid4()
    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), "world/level.dat": b"\x00"})
    server = await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    imported_plugins = await dst_uow.plugins.list_for_server(server.id)
    assert imported_plugins == []


# --- import: Bedrock port allocation on Geyser detection (issue #1551) -------


async def _export_archive(
    *, community: uuid.UUID, mod_identifier: str | None = None
) -> bytes:
    """Build an export archive from a fresh source server.

    ``mod_identifier`` seeds a single plugin row on the source server (e.g.
    ``"Geyser-Spigot"``); ``None`` (the default) exports with no plugins.
    """

    src_id = uuid.uuid4()
    src_uow = FakeUnitOfWork()
    src_uow.servers.seed(_server(community_id=community, server_id=src_id))
    if mod_identifier is not None:
        src_uow.plugins.seed(_plugin(server_id=src_id, mod_identifier=mod_identifier))
    export = ExportServer(
        uow=src_uow, clock=FakeClock(_NOW), file_store=FakeFileStore()
    )
    return await _drain(
        await export(community_id=CommunityId(community), server_id=ServerId(src_id))
    )


async def test_import_with_geyser_allocates_bedrock_port() -> None:
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        bedrock_port_range=_BEDROCK_RANGE,
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )
    assert dst_uow.servers.by_id[server.id].bedrock_port == 19132


async def test_import_without_geyser_does_not_allocate_bedrock_port() -> None:
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="fabric-api")

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        bedrock_port_range=_BEDROCK_RANGE,
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )
    assert dst_uow.servers.by_id[server.id].bedrock_port is None


async def test_import_geyser_without_gate_leaves_port_unset() -> None:
    # bedrock_port_range None = the deployment gate is off (relay disabled or no
    # Bedrock capability): a re-created Geyser plugin must not allocate, but the
    # import itself still succeeds.
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )
    assert dst_uow.servers.by_id[server.id].bedrock_port is None
    imported_plugins = await dst_uow.plugins.list_for_server(server.id)
    assert len(imported_plugins) == 1


async def test_import_geyser_exhausted_bedrock_window_is_seed_failed() -> None:
    # Window exhaustion during the plugin-metadata import is a post-commit
    # failure (the server row and its working set already committed): it reuses
    # the #243/#252 seed-failure posture rather than the install paths' distinct
    # ``bedrock_port_range_exhausted``, since -- unlike an install-time abort --
    # a row already exists here.
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    other = _server(community_id=community, server_id=uuid.uuid4())
    other.bedrock_port = 19132
    dst_uow.servers.seed(other)
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        bedrock_port_range=PortRange(start=19132, end=19132),
    )
    with pytest.raises(WorkingSetSeedFailedError):
        await imp(
            community_id=CommunityId(community),
            name="imported",
            content=archive,
        )
    # The row and its working set are already committed by the time the plugin
    # import runs (one commit, from create_server); the plugin-import transaction
    # itself never reaches its own commit -- the abort signal is that the commit
    # count stops at 1 (the real UoW rolls the failed transaction back; the fake's
    # staged plugin add is not transactional, mirroring the install-path
    # exhaustion test's same caveat).
    [server] = [s for s in dst_uow.servers.by_id.values() if s.name.value == "imported"]
    assert server.bedrock_port is None
    assert dst_uow.commits == 1


async def test_import_geyser_port_racer_is_seed_failed() -> None:
    # A concurrent allocation racer hitting the UNIQUE(bedrock_port) backstop
    # (issue #1550: the adapter translates the constraint violation to
    # PortAlreadyTakenError at the UPDATE execute site) maps to the same
    # post-commit seed-failure posture as exhaustion -- 503 ``seed_failed``,
    # never an unmapped 500 -- since the server row is already committed here,
    # unlike the install paths' pre-commit 409 ``bedrock_port_taken``.
    class _RacerServerRepository(FakeServerRepository):
        # On this path ``update`` is called only by the Bedrock allocation write
        # (CreateServer uses ``add``), so raising here models the backstop firing.
        async def update(self, server: Server) -> None:
            raise PortAlreadyTakenError(str(server.bedrock_port))

    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow = FakeUnitOfWork(servers=_RacerServerRepository())
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        bedrock_port_range=_BEDROCK_RANGE,
    )
    with pytest.raises(WorkingSetSeedFailedError):
        await imp(
            community_id=CommunityId(community),
            name="imported",
            content=archive,
        )
    # Same abort signal as the exhaustion test: the plugin-import transaction
    # never reaches its own commit (count stays at 1, from create_server); the
    # real UoW rolls back the staged plugin rows and the failed port UPDATE
    # together (the fake's staged in-memory state is not transactional).
    assert dst_uow.commits == 1
